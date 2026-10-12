"""交易分类器（两层结构）。

为什么必须分两层（这是实测踩出来的）：

    第一版只用了关键词规则，结果微信里 103 笔「扫二维码付款」全部被归成"收入"。
    原因是这些记录的商品字段写的是「收款方备注:二维码收款」，命中了「收款」这个关键词。
    但它们是实实在在的**支出**。

    → 教训：资金方向（收入 / 支出 / 资金调拨）是**事实**，账单里已经明确标了，
      不该让关键词去猜。所以第一层按 direction 直接判定，第二层才对"支出"
      做商户/商品的关键词归类。

第一层（方向层，最高优先）：
    direction='in'      → 收入      （category_code=INCOME）
    direction='neutral' → 金融理财  （充值/提现/理财通存取，属资金调拨，非消费）
    direction='out'     → 交给第二层

第二层（关键词层）：
    规则存在 t_category_rule，priority 越小越先命中：
        1  方向性动作词  转账 / 红包 / 退款
        10 精确商户词    瑞幸 / 蜜雪冰城 / 摩拜 …
        50 品类词        牛奶 / 纸抽 / 话费 …
        90 平台词        淘宝 / 拼多多 / 抖音   ← 兜底，因为平台卖什么都可能
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple


class Classifier:
    """把交易记录归到 t_category。"""

    def __init__(self, rules: List[Tuple[int, int, str, str]],
                 codes: Optional[Dict[str, int]] = None):
        # rules: [(priority, category_id, keyword, match_field), ...] 已按优先级排好
        self.rules = rules
        # category_code -> category_id，用于方向层直接定位
        self.codes = codes or {}

    # -----------------------------------------------------------
    @classmethod
    def from_db(cls, conn) -> "Classifier":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT priority, category_id, keyword, match_field "
                "FROM t_category_rule WHERE is_enabled = 1 "
                "ORDER BY priority ASC, CHAR_LENGTH(keyword) DESC"
            )
            rules = list(cur.fetchall())
            cur.execute("SELECT category_code, category_id FROM t_category")
            codes = {code: cid for code, cid in cur.fetchall()}
        return cls(rules, codes)

    # -----------------------------------------------------------
    def classify(self, counterparty: str, product_desc: str,
                 direction: str = "out") -> Tuple[Optional[int], str]:
        """返回 (category_id, category_src)。

        未命中返回 (None, 'uncategorized')，留给人工补规则 ——
        不硬塞进"其他"，否则"其他"会变成垃圾筐，掩盖分类规则的缺口。
        """
        # ---- 第一层：方向层 ----
        if direction == "in":
            return self.codes.get("INCOME"), "rule"
        if direction == "neutral":
            return self.codes.get("FINANCE"), "rule"

        # ---- 第二层：支出 → 关键词规则 ----
        cp = counterparty or ""
        pd = product_desc or ""
        for _priority, cid, kw, field in self.rules:
            if field == "counterparty" and kw in cp:
                return cid, "rule"
            if field == "product" and kw in pd:
                return cid, "rule"
            if field == "both" and (kw in cp or kw in pd):
                return cid, "rule"
        return None, "uncategorized"

    # -----------------------------------------------------------
    def explain(self, counterparty: str, product_desc: str,
                direction: str = "out") -> Dict:
        """调试用：说明是哪一层、哪条规则命中的。"""
        if direction == "in":
            return {"layer": "direction", "rule": "direction=in → 收入"}
        if direction == "neutral":
            return {"layer": "direction", "rule": "direction=neutral → 金融理财"}
        cp, pd = counterparty or "", product_desc or ""
        for priority, cid, kw, field in self.rules:
            hit_cp = field in ("counterparty", "both") and kw in cp
            hit_pd = field in ("product", "both") and kw in pd
            if hit_cp or hit_pd:
                return {"layer": "keyword", "category_id": cid, "keyword": kw,
                        "field": "counterparty" if hit_cp else "product",
                        "priority": priority}
        return {"layer": "none"}
