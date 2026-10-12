"""账单导入的公共契约层。

三份账单（微信 / 支付宝 / 工商银行）字段各异，在这里统一成一种中间形态
RawRecord，后续入库、分类、特征计算都只认这一种结构。

设计要点：
  1. 金额恒为正，方向由 direction 表达 —— 避免"负号"这种隐式语义散落在各处
  2. 去重键不用平台订单号 —— 微信/支付宝订单号格式不同、且支付宝订单号带制表符，
     银行流水根本没有订单号。改用行指纹，一行内容变一个字指纹就变。
  3. 判定"是否退款行"集中在 normalize_status()，因为两家平台的措辞都不一样
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------
# 中间形态
# ---------------------------------------------------------------
@dataclass
class RawRecord:
    """一份账单文件里的一行，已归一化。"""

    source: str                      # wechat / alipay / bank
    trade_time: datetime             # 交易时间
    amount: float                    # 金额，恒为正
    direction: str                   # out / in / neutral
    counterparty: str = ""           # 交易对方
    product_desc: str = ""           # 商品说明 / 摘要
    pay_method: str = ""             # 支付方式
    trade_status: str = ""           # 原始状态文本
    trade_no: Optional[str] = None   # 平台订单号（可能为空）
    is_refund: bool = False          # 该行本身是退款/关闭记录，不参与支出合计
    remark: str = ""

    # is_primary=True 计入消费统计；False 表示这是另一份账单的镜像行（防跨源双计）
    is_primary: bool = True

    dedup_key: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.amount = round(abs(float(self.amount)), 2)
        self.counterparty = clean_text(self.counterparty)
        self.product_desc = clean_text(self.product_desc)
        self.pay_method = clean_text(self.pay_method)
        self.trade_status = clean_text(self.trade_status)
        self.trade_no = clean_text(self.trade_no) or None
        self.remark = clean_text(self.remark)
        self.is_primary = not is_funding_mirror(self.source, self.direction, self.counterparty)
        self.dedup_key = make_dedup_key(
            self.source, self.trade_time, self.amount,
            self.counterparty, self.product_desc, self.trade_no,
        )


def clean_text(v) -> str:
    """清掉平台导出里的脏字符。

    支付宝 CSV 的订单号后面带一个制表符（\\t），是全角导出留下的尾巴；
    还有各种不间断空格。不处理的话这些字符会跟着进数据库并污染指纹。
    """
    if v is None:
        return ""
    s = str(v)
    s = s.replace("\t", "").replace("\u3000", " ").replace("\xa0", " ")
    s = s.replace("\r", "").replace("\n", " ")
    return s.strip()


def make_dedup_key(source: str, trade_time: datetime, amount: float,
                   counterparty: str, product_desc: str,
                   trade_no: Optional[str] = None) -> str:
    """行指纹 = MD5(来源 + 时间 + 金额 + 对方 + 商品说明 [+ 平台订单号])。

    主体是五个"两端平台都必然提供"的字段，订单号只在**存在时**追加。

    为什么订单号不能省（实测教训）：
        微信实测 933 行中有两笔「中铁网络 ¥279.50」时间精确到秒完全相同、
        对方与商品名也完全一致，但交易单号不同 —— 它们是两笔各自独立的退款。
        只用五字段做主键会把这两笔合成一笔，直接丢数据。

    为什么订单号也不能当地唯一键（另一半教训）：
        银行流水根本没有订单号；支付宝订单号尾部带制表符；不同平台格式互不相通。
        所以它只能作为"有则加强"的补充，不能作为唯一的判重依据。
    """
    base = f"{source}|{trade_time:%Y-%m-%d %H:%M:%S}|{amount:.2f}|{counterparty}|{product_desc}"
    if trade_no:
        base = f"{base}|{trade_no}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------
# 方向 / 状态归一
# ---------------------------------------------------------------
_DIR_OUT = ("支出", "支", "out", "付款", "借")
_DIR_IN = ("收入", "收", "in", "收款", "贷")
_DIR_NEUTRAL = ("不计收支", "中性交易", "/", "其他", "-", "")


def normalize_direction(text: str) -> str:
    """把两家平台各自的"收/支"措辞统一成 out / in / neutral。"""
    s = clean_text(text)
    if s in _DIR_OUT:
        return "out"
    if s in _DIR_IN:
        return "in"
    if s in _DIR_NEUTRAL:
        return "neutral"
    # 兜底：按包含关系判断，避免平台换措辞后整批方向判错
    if any(k in s for k in ("支出", "支")):
        return "out"
    if any(k in s for k in ("收入", "收")):
        return "in"
    return "neutral"


# 无法撤销的交易状态：这些行不能计入支出
_DEAD_STATUS = ("交易关闭", "已关闭", "已撤销", "支付失败", "已失效")
_REFUND_HINT = ("退款", "退回", "退")


def judge_refund(status: str, trade_type: str = "") -> bool:
    """判断这一行是不是"退款/作废"记录。

    两个来源都要看：
      · 状态字段 —— 微信叫「已全额退款」「已退款¥10.00」，支付宝叫「退款成功」
      · 类型字段 —— 微信把退款做成了独立交易类型「美团平台商户-退款」
    这些行若当作普通支出计入，总额会虚高（本项目实测微信 662 笔支出里有 71 笔属于此类）。
    """
    s = clean_text(status)
    t = clean_text(trade_type)
    if any(k in s for k in ("退款", "已退", "退回")):
        return True
    if any(k in t for k in ("退款", "退回")):
        return True
    return False


def judge_dead(status: str) -> bool:
    """交易关闭 / 支付失败 —— 这行根本没发生，直接丢弃不入库。"""
    s = clean_text(status)
    return any(k in s for k in _DEAD_STATUS)


# ---------------------------------------------------------------
# 跨源重复（资金镜像）识别
# ---------------------------------------------------------------
# 支付通道聚合方。银行流水里只要对方是这些主体，就说明那笔钱是
# **经由微信/支付宝付出去的**，渠道账单里已经记过一次；银行侧这条
# 只是同一笔钱在资金链上的镜像，计入消费统计就会双计。
#
# 实测依据（本项目真实数据）：
#   工行「消费」102 笔 ¥2,111.17 中，
#     36 笔 ¥1,139.86 对方为「支付宝（中国）网络技术有限公司」
#      2 笔 ¥  237.00 对方为财付通 / 微信转账
#   —— 这 38 笔 ¥1,376.86 全部与渠道账本重复，占工行"消费"的 65%。
#   剩余 64 笔 ¥734.31 是绑卡直付（抖音/拼多多/美团等 App 内直接扣卡），
#   渠道账单看不到，属于工行独有的有效信息，应当保留。
#
# 注意「财付通」既是微信支付的通道方，也是零钱通/理财通的通道方。
# 所以此规则只在**支出方向**上判定镜像；收入方向的「零钱通提现」
# 是资金回笼，不是任何消费的镜像，不能一并剔除。
CHANNEL_AGGREGATORS = ("支付宝", "财付通", "微信转账", "微信支付")


def is_funding_mirror(source: str, direction: str, counterparty: str) -> bool:
    """判断这一行是否为渠道账单的银行侧镜像（应排除出消费统计）。"""
    if source != "bank" or direction != "out":
        return False
    cp = clean_text(counterparty)
    return any(k in cp for k in CHANNEL_AGGREGATORS)


# ---------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------
_TIME_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
)


def parse_time(v) -> Optional[datetime]:
    """尽量宽松地解析时间。解析不出返回 None，由调用方决定跳过还是报错。"""
    if isinstance(v, datetime):
        return v
    s = clean_text(v)
    if not s:
        return None
    for fmt in _TIME_PATTERNS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 处理 "2025-09-04 17:44:53.0" 这类带毫秒的
    m = re.match(r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2})[ T](\d{1,2}:\d{2}(?::\d{2})?)", s)
    if m:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(f"{m.group(1).replace('/', '-')} {m.group(2)}", fmt)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------
# 文件指纹
# ---------------------------------------------------------------
def file_md5(path: str, chunk: int = 1 << 20) -> str:
    """算文件 MD5，用于 t_import_batch 防重复导入。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
