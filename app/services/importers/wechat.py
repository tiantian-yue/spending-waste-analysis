"""微信支付账单解析器。

源文件形态（2026-10 实测）：
    Sheet1，A1:K951
    第 1-16 行是账户说明与汇总（「共933笔记录」「收入：228笔 …」）
    第 17 行是分隔线「-----微信支付账单明细列表-----」
    第 18 行才是表头：交易时间 | 交易类型 | 交易对方 | 商品 | 收/支 |
                     金额(元) | 支付方式 | 当前状态 | 交易单号 | 商户单号 | 备注
    第 19 行起是数据

踩过的坑：
    · 表头不在第 1 行 —— 不能直接按固定行号取，要扫描定位
    · 金额列是数值型（int/float 混用），不是字符串
    · 退款在微信里是"独立交易类型"（如「美团平台商户-退款」），
      同时也体现在「当前状态」（如「已全额退款」），两处都要判
    · 支付方式里有「/」占位，代表该交易不涉及支付渠道（如收入）
"""

from __future__ import annotations

from typing import List

from .base import RawRecord, judge_dead, judge_refund, normalize_direction, parse_time

SOURCE = "wechat"
HEADER_ANCHOR = "交易时间"

# 表头名 → 我们的字段名
COLUMN_MAP = {
    "交易时间": "trade_time",
    "交易类型": "trade_type",
    "交易对方": "counterparty",
    "商品": "product_desc",
    "收/支": "direction_raw",
    "金额(元)": "amount",
    "支付方式": "pay_method",
    "当前状态": "trade_status",
    "交易单号": "trade_no",
    "商户单号": "merchant_no",
    "备注": "remark",
}


def _find_header(rows: list) -> int:
    """扫描定位表头行。返回行下标；找不到抛错而不是静默猜 0。"""
    for i, row in enumerate(rows):
        if row and str(row[0] or "").strip() == HEADER_ANCHOR:
            return i
    raise ValueError("微信账单里找不到表头行（首列应为「交易时间」），文件格式可能已变更")


def parse(path: str) -> List[RawRecord]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))

    h = _find_header(rows)
    header = [str(c or "").strip() for c in rows[h]]
    col = {COLUMN_MAP[name]: idx for idx, name in enumerate(header) if name in COLUMN_MAP}
    if "trade_time" not in col or "amount" not in col:
        raise ValueError(f"微信账单表头缺少必需列，实得：{header}")

    out: List[RawRecord] = []
    for row in rows[h + 1:]:
        if not row or row[0] in (None, ""):
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

        out.append(RawRecord(
            source=SOURCE,
            trade_time=t,
            amount=float(g("amount") or 0),
            direction=normalize_direction(str(g("direction_raw") or "")),
            counterparty=str(g("counterparty") or ""),
            product_desc=str(g("product_desc") or ""),
            pay_method=str(g("pay_method") or ""),
            trade_status=status,
            trade_no=str(g("trade_no") or "") or None,
            is_refund=judge_refund(status, str(g("trade_type") or "")),
            remark=str(g("remark") or ""),
        ))
    return out
