# -*- coding: utf-8 -*-
"""把拼多多商品回填结果固化为可复现的 SQL 补丁。

为什么要有这一步
----------------
`match_pdd_to_bills.py` 得出的品类建议只存在于我本机的 CSV 里，别人 clone 仓库
重跑导入流程，那 700 多笔「先用后付」仍然只会落在「电商综合」。所以必须把结论
**固化成 SQL**，让「重建库 → 导入 → 执行补丁」三步能复现完全一样的库。

定位键的选择
------------
不用 trans_id（自增，重建库后不可靠），改用 `(trade_time, amount)` —— 实测
716 笔「先用后付」里该组合 **716 笔全唯一**（见 README 数据说明），
再加 `product_desc LIKE '%先用后付%'` 与 `category_id = <ECOMM>` 双保险。

不写入的记录
------------
建议大类为「其他」或没有候选的，**保持「电商综合」不动**。理由：这些笔确实是
拼多多买的、但页面信息不足以判断品类，`ECOMM`（"平台账单无法细分品类"）正是
它们的真实状态；写进 `OTHER` 反而会污染「其他」这个缺口信号。

用法
----
    python scripts/gen_pdd_backfill_sql.py data/pdd_match.csv -o sql/03_backfill_pdd.sql
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)          # 让 `from app.services...` 能被找到

ECOMM_ID = 12          # sql/02_seed.sql 中电商综合的 category_id
SKIP_CODES = {"", "OTHER", "ECOMM"}

HEADER = """-- =============================================================
-- 03_backfill_pdd.sql   拼多多订单品类回填补丁
-- -------------------------------------------------------------
-- ⚠ 执行顺序：01_schema.sql → 02_seed.sql → scripts/import_bills.py → 本文件
--
-- 背景
--   账单里拼多多的扣款只写「先用后付」，商品字段没有任何品类信息，
--   因此 1015 笔电商综合里 716 笔无法细分。这些订单的真实商品已从
--   拼多多订单页的页面原文还原（scripts/parse_pdd_orders.py），
--   再按「金额 + 先用后付标签 + 顺序对齐」匹配回交易
--   （scripts/match_pdd_to_bills.py），本文件是匹配结果的固化。
--
-- 口径影响（数字为本次数据快照，2026-10-14）
--   回填后「电商综合」从 1015 笔降到 {left} 笔；其中 {groc} 笔食品生鲜
--   （is_durable = 0）随之移出「实物类」标注候选池 —— 食品不存在
--   "买回来放着不用"的可能，留在池里会稀释闲置率。
--
-- 幂等性
--   每条 UPDATE 都带 `category_id = {ecomm}` 条件，重复执行不会二次改写。
-- =============================================================

USE spending_waste;

"""

TAIL = """
-- -------------------------------------------------------------
-- 执行后自检（预期：电商综合笔数 = {left}，且无 category_id IS NULL 的消费）
-- -------------------------------------------------------------
-- SELECT c.category_code, COUNT(*) n, ROUND(SUM(t.amount),2) amt
--   FROM t_transaction t JOIN t_category c ON c.category_id = t.category_id
--  WHERE t.direction='out' AND t.is_primary=1 AND t.is_refund=0
--  GROUP BY 1 ORDER BY n DESC;
"""


def load_rows(path: str) -> list[dict]:
    with io.open(path, encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def key_of(row: dict) -> str:
    """与 SQL 侧 CONCAT(DATE_FORMAT(trade_time,'%Y-%m-%d %H:%i:%s'),'|',amount) 对齐"""
    return f"{row['交易时间']}|{float(row['金额']):.2f}"


def build(rows: list[dict], code2id: dict[str, int]) -> tuple[str, dict]:
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for r in rows:
        code = (r.get("项目分类码") or "").strip()
        if code in SKIP_CODES:
            continue
        groups[code].append(key_of(r))

    out = [HEADER.format(left=1015 - sum(len(v) for v in groups.values()),
                         groc=len(groups.get("GROC", [])),
                         ecomm=ECOMM_ID)]
    stat = {}
    for code in sorted(groups, key=lambda c: -len(groups[c])):
        keys = sorted(groups[code])
        cat_id = code2id.get(code)
        if cat_id is None:
            raise SystemExit(f"分类码 {code} 在 t_category 里不存在，请先补 02_seed.sql")
        stat[code] = len(keys)
        out.append(f"-- {code}：{len(keys)} 笔")
        out.append(f"UPDATE t_transaction SET category_id = {cat_id}")
        out.append(f" WHERE category_id = {ECOMM_ID}")
        out.append("   AND product_desc LIKE '%先用后付%'")
        out.append("   AND CONCAT(DATE_FORMAT(trade_time,'%Y-%m-%d %H:%i:%s'),'|',amount) IN (")
        for i, k in enumerate(keys):
            comma = "," if i < len(keys) - 1 else ""
            out.append(f"     '{k}'{comma}")
        out.append("   );")
        out.append("")

    out.append(TAIL.format(left=1015 - sum(stat.values())))
    return "\n".join(out), stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("match_csv", nargs="?", default="data/pdd_match.csv")
    ap.add_argument("-o", "--out", default="sql/03_backfill_pdd.sql")
    args = ap.parse_args()

    # 分类码 → id 从库里读，避免把自增 id 写死在脚本里
    try:
        from app.services.importers import pipeline
        conn = pipeline.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT category_code, category_id FROM t_category")
            code2id = {r[0]: r[1] for r in cur.fetchall()}
        conn.close()
    except Exception as exc:                                   # noqa: BLE001
        print(f"读库失败（{exc}），改用内置的种子 id 映射")
        code2id = {"FOOD": 1, "GROC": 14, "TRAFFIC": 2, "APPAREL": 3, "DIGITAL": 4,
                   "STUDY": 5, "LIFE": 6, "MED": 7, "ENT": 8, "HOBBY": 9,
                   "TRAVEL": 11, "ECOMM": 12, "SERVICE": 13, "OTHER": 10}

    rows = load_rows(args.match_csv)
    sql, stat = build(rows, code2id)

    with io.open(args.out, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(sql)

    total = sum(stat.values())
    print(f"匹配表 {len(rows)} 笔 → 回填 {total} 笔，保持电商综合 {len(rows) - total} 笔")
    for code, n in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"   → {code:<8} {n:4d} 笔  (category_id={code2id[code]})")
    print(f"输出：{os.path.abspath(args.out)}  ({len(sql.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
