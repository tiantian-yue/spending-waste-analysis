"""支付宝交易明细 CSV 解析器。

源文件形态（2026-10 实测）：
    编码 **GBK**（用 UTF-8 读会在第 86 字节就崩）
    第 1-21 行是导出信息 + 特别提示 + 汇总（「共60笔记录」）
    第 23 行是「------支付宝支付科技有限公司  电子客户回单------」
    第 24 行才是表头：
      交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,
      收/付款方式,交易状态,交易订单号,商家订单号,备注,     ← 末尾还有一个空列
    第 25 行起是数据

踩过的坑：
    · 编码是 GBK，不是 UTF-8 —— 必须显式指定
    · 表头在第 24 行，且行尾多一个逗号（多出一列空列）
    · **交易订单号 / 商家订单号 字段尾部带一个制表符 \\t** —— 实测 60/60 行全中，
      不 strip 就会带着脏字符进库，并把行指纹算错
    · 交易状态有三种：交易成功 / 交易关闭 / 退款成功
      「交易关闭」要整行丢弃，「退款成功」标记为退款行
    · 有独立的「退款」交易分类（实测 7 笔），也要判为退款
"""

from __future__ import annotations

import csv
import io
from typing import List

from .base import RawRecord, clean_text, judge_dead, judge_refund, normalize_direction, parse_time

SOURCE = "alipay"
ENCODING = "gb18030"
HEADER_PREFIX = "交易时间,"

COLUMN_MAP = {
    "交易时间": "trade_time",
    "交易分类": "trade_type",
    "交易对方": "counterparty",
    "对方账号": "counterparty_acct",
    "商品说明": "product_desc",
    "收/支": "direction_raw",
    "金额": "amount",
    "收/付款方式": "pay_method",
    "交易状态": "trade_status",
    "交易订单号": "trade_no",
    "商家订单号": "merchant_no",
    "备注": "remark",
}


def _read_lines(path: str) -> List[str]:
    """按 GBK 家族解码，返回行列表。

    先试 gb18030（GBK 的超集，能覆盖更多生僻字），失败再退 gbk。
    """
    raw = open(path, "rb").read()
    for enc in (ENCODING, "gbk", "utf-8-sig"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"支付宝账单编码无法识别（已试 gb18030 / gbk / utf-8-sig）：{path}")


def _find_header(lines: List[str]) -> int:
    for i, l in enumerate(lines):
        if l.startswith(HEADER_PREFIX):
            return i
    raise ValueError("支付宝账单里找不到表头行（应以「交易时间,」开头），文件格式可能已变更")


def parse(path: str) -> List[RawRecord]:
    lines = _read_lines(path)
    h = _find_header(lines)
    header = [clean_text(c) for c in lines[h].split(",")]
    col = {COLUMN_MAP[name]: idx for idx, name in enumerate(header) if name in COLUMN_MAP}

    out: List[RawRecord] = []
    # 只把表头及之后的行交给 csv 模块，避免前面的账户说明被当成数据
    reader = csv.reader(io.StringIO("\n".join(lines[h:])))
    next(reader, None)  # 跳过表头

    for row in reader:
        if not row or not clean_text(row[0]):
            continue

        def g(key, default=""):
            i = col.get(key)
            return row[i] if i is not None and i < len(row) else default

        t = parse_time(g("trade_time"))
        if t is None:
            continue

        status = str(g("trade_status") or "")
        if judge_dead(status):
            continue

        # 支付宝订单号尾部带制表符，clean_text 会处理；这里再显式取一次保证语义清晰
        trade_no = clean_text(g("trade_no")) or None

        out.append(RawRecord(
            source=SOURCE,
            trade_time=t,
            amount=float(clean_text(g("amount")) or 0),
            direction=normalize_direction(str(g("direction_raw") or "")),
            counterparty=str(g("counterparty") or ""),
            product_desc=str(g("product_desc") or ""),
            pay_method=str(g("pay_method") or ""),
            trade_status=status,
            trade_no=trade_no,
            is_refund=judge_refund(status, str(g("trade_type") or "")),
            remark=str(g("remark") or ""),
        ))
    return out
