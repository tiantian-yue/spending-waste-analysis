"""从「拼多多品类回填.csv」生成商品名补全 SQL（sql/05_goods_detail.sql）。

为什么要有这一步
----------------
账单在电商这笔上不记商品名：拼多多只有
    对方 = 拼多多平台商户 / 拼多多支付
    商品说明 = 先用后付 / 消费 / /
实测 776 条标注候选里 653 条是拼多多、523 条商品说明是占位文本。
标注时看到"¥12.80 先用后付"根本回忆不起买了什么 → 闲置标签会变成瞎猜。

商品名的唯一可用来源是**拼多多订单列表页**（用户自己导出）。上一轮已经：
    解析兜底原文 → parse_pdd_orders.py
    商品标题归类 → classify_pdd_goods.py
    (时间,金额) 对齐账单 → match_pdd_to_bills.py
产物就是 拼多多品类回填.csv，里面 匹配商品（判定用） 一列就是真实商品标题。

本脚本把它落库到 t_goods_detail（派生表，见 sql/01_schema.sql 第 9 节）。
**不改 t_transaction.product_desc** —— 它是 dedup_key 的输入，改了会破坏去重。

定位键为什么用 (trade_time, amount) 而不是 trans_id
----------------------------------------------------
trans_id 是自增的，重建库后会变；trade_time+amount 在 716 条上实测全唯一。
与 sql/03_backfill_pdd.sql 保持同一套定位约定。

用法
----
    python scripts/gen_goods_detail_sql.py
    python scripts/gen_goods_detail_sql.py --csv ../拼多多品类回填.csv --out sql/05_goods_detail.sql
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.importers import pipeline                        # noqa: E402

DEFAULT_CSV = (
    "../拼多多品类回填.csv",        # ① 先用后付口径（716 笔，高置信 94.6%）
    "data/pdd_match_all.csv",       # ② 全量拼多多口径（991 笔，补 ① 的缺口）
)
DEFAULT_DETAIL_CSV = "../拼多多订单明细.csv"
DEFAULT_OUT = "sql/05_goods_detail.sql"
# 回填 CSV 的「确定性」→ t_goods_detail.match_confidence 枚举
CONF_MAP = {"唯一": "unique", "同类": "same_cat", "对齐": "ordered", "跨类": "fuzzy"}


def load_backfill(paths: list[str]) -> tuple[dict, dict]:
    """读多份回填 CSV，按 (交易时间, 金额) 合并。**靠前优先级高，先到先得**。

    为什么要两份：
      ① 先用后付口径的匹配质量最高（候选池窄 → 对齐成功率高，94.6% 高置信），
         但它只覆盖 716 笔，剩下的拼多多交易商品名是空的；
      ② 全量口径扩大到 991 笔，能补上缺口，代价是候选池变宽、跨类比例升高。
    合成一份时让 ① 先占坑，② 只填 ① 没覆盖到的，两边优点都拿到。
    """
    merged: dict[tuple[str, str], dict] = {}
    src_cnt: dict[str, int] = {}
    for p in paths:
        if not os.path.exists(p):
            print(f"  [跳过] 找不到 {p}")
            continue
        n = 0
        with io.open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = (r["交易时间"].strip(), f"{float(r['金额']):.2f}")
                if key in merged:
                    continue
                merged[key] = r
                n += 1
        src_cnt[os.path.basename(p)] = n
    return merged, src_cnt


def esc(v: str) -> str:
    """单引号转义，空串给 NULL（SQL 里 '' 与 NULL 语义不同，商品名空就是没补上）。"""
    if v is None:
        return "NULL"
    v = v.replace("\\", "\\\\").replace("'", "''").strip()
    return f"'{v}'" if v else "NULL"


def load_spec_index(path: str) -> dict:
    """从订单明细建 (店铺, 商品标题) → 规格 的索引，用于顺带补上规格。"""
    if not os.path.exists(path):
        return {}
    idx = {}
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            idx[(r["店铺"].strip(), r["商品标题"].strip())] = r.get("规格", "").strip()
    return idx


def parse_shop(candidates: str, title: str) -> str:
    """从「全部候选」里挑出标题命中的那条，取 '·' 之前的店铺名。

    格式：店铺·标题[大类] ;; 店铺·标题[大类] ;; ...
    """
    if not candidates or not title:
        return ""
    for cand in candidates.split(" ;; "):
        cand = cand.strip()
        if not cand:
            continue
        body = cand.rsplit("[", 1)[0]          # 去掉尾部 [大类]
        if body.endswith(title) or title in body:
            return body.split("·", 1)[0].strip()
    return ""


def parse_alt_titles(candidates: str, limit: int = 480) -> str:
    """从「全部候选」抽出全部候选标题，用 ' / ' 连接（匹配没定夺时给人挑）。

    ★ 这 39 笔是匹配器**故意没下结论**的（金额相同有多个候选、且跨类）。
      与其丢掉，不如把候选原样交给人工 —— 标注本来就是人来做的事。
    """
    if not candidates:
        return ""
    out, seen = [], set()
    for cand in candidates.split(" ;; "):
        body = cand.strip().rsplit("[", 1)[0]
        title = body.split("·", 1)[1].strip() if "·" in body else body
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(title)
    s = " / ".join(out)
    return s[:limit]


def fetch_pdd_trans(conn) -> dict:
    """取出全部拼多多交易，建 (时间, 金额) → 交易 的索引。"""
    sql = """
        SELECT t.trans_id,
               DATE_FORMAT(t.trade_time, '%Y-%m-%d %H:%i:%s') AS tm,
               t.amount, t.counterparty, t.product_desc
        FROM t_transaction t
        WHERE t.counterparty LIKE '拼多多%'
        ORDER BY t.trade_time
    """
    idx = collections.defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(sql)
        for trans_id, tm, amount, cp, pd in cur.fetchall():
            idx[(tm, f"{float(amount):.2f}")].append(
                {"trans_id": int(trans_id), "cp": cp, "pd": pd})
    return idx


def main() -> int:
    ap = argparse.ArgumentParser(description="生成商品名补全 SQL")
    ap.add_argument("--csv", nargs="+", default=list(DEFAULT_CSV),
                    help="一份或多份回填 CSV，靠前优先级高（先到先得）")
    ap.add_argument("--detail", default=DEFAULT_DETAIL_CSV)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def abspath(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(here, p)

    back, src_cnt = load_backfill([abspath(p) for p in args.csv])
    spec_idx = load_spec_index(abspath(args.detail))

    conn = pipeline.connect()
    pdd = fetch_pdd_trans(conn)
    conn.close()

    stmts, hits, miss, ambiguous, alt_only = [], [], [], [], []
    for (tm, amt), r in back.items():
        title = r["匹配商品（判定用）"].strip()
        conf = CONF_MAP.get(r["确定性"].strip())
        cands = pdd.get((tm, amt), [])
        if len(cands) != 1:
            (miss if not cands else ambiguous).append((tm, amt, len(cands)))
            continue
        if not title or not conf:
            # 匹配器故意没下结论（金额相同有多个候选且跨类）→ 不猜，把候选原样交出去
            alts = parse_alt_titles(r["全部候选"])
            if not alts:
                continue
            stmts.append(
                "INSERT INTO t_goods_detail (trans_id, shop_name, goods_title, spec, alt_titles, match_confidence, source)\n"
                f"SELECT t.trans_id, NULL, NULL, NULL, {esc(alts)}, 'fuzzy', 'pdd_order'\n"
                "  FROM t_transaction t\n"
                f" WHERE t.trade_time = '{tm}' AND t.amount = {amt}\n"
                "    AND t.counterparty LIKE '拼多多%'\n"
                "ON DUPLICATE KEY UPDATE shop_name=VALUES(shop_name), goods_title=VALUES(goods_title),\n"
                "                        spec=VALUES(spec), alt_titles=VALUES(alt_titles),\n"
                "                        match_confidence=VALUES(match_confidence);")
            alt_only.append((tm, amt, len(alts.split(" / "))))
            continue
        shop = parse_shop(r["全部候选"], title)
        spec = spec_idx.get((shop, title), "")
        stmts.append(
            "INSERT INTO t_goods_detail (trans_id, shop_name, goods_title, spec, alt_titles, match_confidence, source)\n"
            f"SELECT t.trans_id, {esc(shop)}, {esc(title)}, {esc(spec)}, NULL, '{conf}', 'pdd_order'\n"
            "  FROM t_transaction t\n"
            f" WHERE t.trade_time = '{tm}' AND t.amount = {amt}\n"
            "    AND t.counterparty LIKE '拼多多%'\n"
            "ON DUPLICATE KEY UPDATE shop_name=VALUES(shop_name), goods_title=VALUES(goods_title),\n"
            "                        spec=VALUES(spec), alt_titles=VALUES(alt_titles),\n"
            "                        match_confidence=VALUES(match_confidence);")
        hits.append((tm, amt, conf, shop))

    conf_stat = collections.Counter(h[2] for h in hits)
    no_shop = sum(1 for h in hits if not h[3])

    head = f"""-- =============================================================
--  商品名补全  sql/05_goods_detail.sql
--  数据源：拼多多订单列表（用户导出）→ 解析 → (交易时间,金额) 对齐账单
--  落地：t_goods_detail（派生表，可整表重建；不改 t_transaction.product_desc）
--  生成脚本：scripts/gen_goods_detail_sql.py
--  幂等：ON DUPLICATE KEY UPDATE，重复执行结果一致
--
--  本批 {len(stmts)} 条 = 已定商品名 {len(hits)} + 只给候选待人工判断 {len(alt_only)}
--  匹配强度分布：
"""
    for k in ("unique", "same_cat", "ordered"):
        head += f"--    {k:9}{conf_stat.get(k, 0):5} 条\n"
    head += f"""--    fuzzy    {len(alt_only):5} 条（没定夺，写进 alt_titles，工作台带黄标）
--  店铺名解析成功 {len(hits) - no_shop}/{len(hits)}
-- ============================================================

USE spending_waste;
SET NAMES utf8mb4;

"""
    with io.open(os.path.join(here, args.out), "w", encoding="utf-8") as f:
        f.write(head + "\n".join(stmts) + "\n")

    print("来源合并          " + " · ".join(f"{k} {v} 行" for k, v in src_cnt.items()))
    print(f"合并后去重        {len(back)} 笔")
    print(f"生成 INSERT 语句  {len(stmts)} 条 → {args.out}")
    print(f"  已定商品名      {len(hits)} 条 {dict(conf_stat)}")
    print(f"  只给候选        {len(alt_only)} 条"
          f"（平均 {sum(a[2] for a in alt_only) / max(len(alt_only), 1):.1f} 个候选）")
    print(f"  店铺名解析成功  {len(hits) - no_shop}/{len(hits)}")
    print(f"账单侧无对应交易  {len(miss)} 条 {miss}")
    print(f"账单侧多笔命中    {len(ambiguous)} 条 {ambiguous}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
