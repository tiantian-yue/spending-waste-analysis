"""中国工商银行借记卡历史明细（PDF）解析器。

源文件形态（2026-10 实测）：
    9 页 PDF，OpenPDF 生成。每页都重复页眉页脚：
        「请扫描二维码 / 识别明细真伪 / 中国工商银行借记账户历史明细（电子版）
          / 卡号 6222… 户名：… 起止日期：… / 交易日期 账号 储种 …」
    正文每一条记录占 **两行**：
        第 1 行：交易日期 YYYY-MM-DD
        第 2 行起：时间 账号 储种 序号 币种 钞汇 摘要 地区 金额 余额 对方户名 对方账号 渠道
    且「对方户名」过长时会折行，「渠道」也常被折到下一行。

踩过的坑（这些是本解析器的核心难点）：
    · 记录以"纯日期行"为界，不能用固定行号
    · 折行拼接有两种情况，必须区别对待：
        - 下一行开头是账号掩码（如 0502****5928）或「（空）」→ 上一个字段已结束，补空格
        - 下一行开头是汉字（如「科技有限公司」）→ 是被截断的户名，**不能补空格**
      如果用统一方式拼接，要么把户名拆开、要么把户名和账号粘连
    · 金额带千分位逗号与正负号（-9,000.00），且与"余额"列相邻，正则要锚定准确
    · 摘要是判断资金性质的关键（消费 / 理财 / 缴费 / 转账 …），必须单独取出：
      其中「理财」是转入微信零钱通或理财通，**不是消费**，误算会严重虚高支出
"""

from __future__ import annotations

import re
from typing import List, Optional

from .base import RawRecord, clean_text, parse_time

SOURCE = "bank"

# 页眉页脚，出现一次就丢一次
_NOISE = re.compile(
    r"^(请扫描二维码|识别明细真伪|中国工商银行借记账户历史明细"
    r"|卡号\s*6222|交易日期\s+账号\s+储种)"
)
_DATE_ONLY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# 正文主体：时间 账号 活期 序号 人民币 钞汇 摘要 地区 金额 余额 尾部
_MAIN = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<acct>\d+)\s+"
    r"(?P<kind>\S+)\s+"
    r"(?P<seq>\d+)\s+"
    r"(?P<ccy>\S+)\s+"
    r"(?P<cash>[钞汇])\s+"
    r"(?P<summary>\S+)\s+"
    r"(?P<region>\d{4})\s+"
    r"(?P<amount>[+-][\d,]+\.\d{2})\s+"
    r"(?P<balance>[\d,]+\.\d{2})\s+"
    r"(?P<tail>.*)$"
)

# 账号掩码 / 空值占位 —— 下一行以它们开头说明上一个字段已经写完
_NEW_FIELD = re.compile(r"^(?:\d{4}\*{4}\d{4}|[（(]空[）)])")

# 渠道关键词，位于记录最末尾
_CHANNELS = ("快捷支付", "网上银行", "中间业务后台方式", "批量业务", "柜面", "手机银行", "ATM")


def _extract_lines(path: str) -> List[str]:
    from pypdf import PdfReader

    txt = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    return [l.rstrip() for l in txt.splitlines() if l.strip() and not _NOISE.match(l)]


def _join_wrapped(lines: List[str]) -> str:
    """把一条记录的多个物理行拼成一个逻辑行。

    拼法取决于下一行的开头：
      · 账号掩码 / （空）  → 是<b>新字段</b>，补空格
      · 其他（汉字为主）    → 是<b>被截断的户名</b>，直接相连，不能补空格
    """
    s = lines[0].strip()
    for nxt in lines[1:]:
        n = nxt.strip()
        if _NEW_FIELD.match(n):
            s += " " + n
        else:
            s += n
    return s


def _split_tail(tail: str) -> tuple[str, str]:
    """从尾部串里切出「对方户名」和「渠道」。

    尾部形如：深圳市财付通支付科技有限公司 2433****0133 快捷支付
             山西财经大学 0502****5928 中间业务后台方式
             （空） （空） 批量业务
    """
    tail = clean_text(tail)
    channel = ""
    for ch in _CHANNELS:
        if tail.endswith(ch):
            channel = ch
            tail = tail[: -len(ch)].strip()
            break
    # 去掉末尾的账号掩码
    tail = re.sub(r"\s*\d{4}\*{4}\d{4}\s*$", "", tail).strip()
    name = tail.replace("（空）", "").replace("(空)", "").strip()
    return name, channel


def parse(path: str) -> List[RawRecord]:
    lines = _extract_lines(path)

    starts = [i for i, l in enumerate(lines) if _DATE_ONLY.match(l.strip())]
    if not starts:
        raise ValueError("工行明细里找不到任何日期行，文件格式可能已变更")

    out: List[RawRecord] = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        # 去掉日期行后拼正文
        body_lines = [l for l in lines[i + 1:end] if l.strip()]
        if not body_lines:
            continue

        joined = _join_wrapped(body_lines)
        m = _MAIN.match(joined)
        if not m:
            continue

        date_str = lines[i].strip()
        t = parse_time(f"{date_str} {m.group('time')}")
        if t is None:
            continue

        amount = float(m.group("amount").replace(",", ""))
        name, channel = _split_tail(m.group("tail"))
        summary = m.group("summary")

        out.append(RawRecord(
            source=SOURCE,
            trade_time=t,
            amount=amount,
            direction="out" if amount < 0 else "in",
            counterparty=name,
            product_desc=summary,          # 银行没有"商品"，用摘要当业务描述
            pay_method=channel,            # 渠道（快捷支付/网上银行…）
            trade_status="交易成功",        # 银行流水是已发生的事实，无"待支付"态
            trade_no=None,                 # 银行流水不含商户订单号
            is_refund=(summary == "退款"),
            remark=f"余额 {m.group('balance')}",
        ))
    return out
