#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把标注候选池里的「服务 / 充值」改判为 SERVICE（生活服务），自动移出闲置标注范围。

为什么需要
----------
`is_durable` 的语义是「这件东西买回来有没有可能闲置」。驾校报名、话费充值
这类**服务**不存在"闲置"——它们要么消费掉了，要么没消费，没有"买回来放着"。
但规则分类器按「商户 + 关键词」猜，把「远景驾校 ¥1,300」「中国广电 ¥520」
落进了 STUDY / LIFE，于是它们带着 is_durable=1 混进了候选池。

代价很实在：候选池里 24 笔 ≥¥100 的大额，有 7 笔（¥1,820，占大额金额 11%）
是这类服务。用户逐笔盘点时，得先替自动分类擦屁股——这是"逐笔盘点"最隐蔽
的成本，也是本次改成品类级体检的动因之一。

判定规则（保守优先，宁漏勿错）
------------------------------
只认**明确的商户名**，或「商品说明明确写着充值/套餐 + 没有真实商品名」：

  1. 商户名命中「驾校 / 运营商 / 影城 / 健身房 …」  → 改判
  2. product_desc 命中「话费 / 首充 / 流量合约 …」
     且 goods_title 为空（说明不是实物商品）        → 改判

不动 goods_title 有值的行 —— 那些是真实商品，「会员」这种词在商品名里
常常只是营销话术（如"多邻国会员年卡"另有归属），不在这里处理。

用法
----
    python scripts/reclass_service.py              # 只扫描，打印报告
    python scripts/reclass_service.py --write-sql  # 顺便生成 sql/07 的 SQL
"""
import argparse
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                      # 让 `app.*` 可导入

from app.services.importers import pipeline  # noqa: E402

# 商户名关键词：只收"一看就是服务"的机构名，不收行业泛词
MERCHANT_KW = (
    "驾校", "驾驶员培训", "广电", "中国移动", "中国联通", "中国电信", "移动通信",
    "联通通信", "电信营业", "网校", "教育科技", "培训中心", "健身房", "影城",
    "电影院", "网吧", "洗浴", "足浴", "按摩", "家政", "保洁", "洗衣店", "干洗",
    "中国石化", "中国石油", "充电桩", "停车场", "快递", "物流", "菜鸟驿站",
)

# 商品说明关键词：只在 goods_title 为空（不是实物商品）时才采信
DESC_KW = (
    "话费", "首充", "流量合约", "赠送流量", "充值", "套餐费", "报名费", "学费",
    "培训费", "押金", "年费", "服务费",
)


def fetch_rows(conn):
    """候选池全量。注意：无参数 execute() 时 pymysql 不还原 %%，这里必须是单 %。"""
    sql = """
        SELECT v.trans_id, DATE_FORMAT(v.trade_time,'%Y-%m-%d') AS tm,
               COALESCE(v.counterparty,'') AS cp,
               COALESCE(v.product_desc,'') AS pd,
               COALESCE(v.goods_title,'')  AS gt,
               v.amount, v.category_code, v.category_name
        FROM v_idle_candidate v
        ORDER BY v.amount DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["amount"] = float(d["amount"])
            out.append(d)
    return out


def judge(r):
    """→ (是否改判, 依据说明)"""
    mh = [k for k in MERCHANT_KW if k in r["cp"]]
    if mh:
        return True, "商户命中:" + mh[0]
    if not r["gt"]:
        dh = [k for k in DESC_KW if k in r["pd"]]
        if dh:
            return True, "说明命中:" + dh[0]
    return False, ""


def main():
    ap = argparse.ArgumentParser(description="候选池里的服务/充值 → 改判 SERVICE")
    ap.add_argument("--write-sql", action="store_true", help="生成 sql/07_service_out.sql")
    ap.add_argument("--out", default="sql/07_service_out.sql")
    args = ap.parse_args()

    conn = pipeline.connect()
    rows = fetch_rows(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT category_id FROM t_category WHERE category_code='SERVICE'")
        cat_service = cur.fetchone()[0]
    conn.close()

    final, kept = [], []
    for r in rows:
        ok, why = judge(r)
        if ok:
            r["_why"] = why
            final.append(r)
        else:
            kept.append(r)

    print(f"候选池 {len(rows)} 笔  ¥{sum(r['amount'] for r in rows):,.2f}")
    print(f"  改判 SERVICE  {len(final):3} 笔  ¥{sum(r['amount'] for r in final):,.2f}")
    print(f"  保持待标      {len(kept):3} 笔  ¥{sum(r['amount'] for r in kept):,.2f}")
    print(f"\n=== 改判清单（全部 {len(final)} 笔，逐笔核对）===")
    for r in final:
        name = (r["gt"] or r["pd"])[:30]
        print(f"  #{r['trans_id']:<5} {r['tm']} ¥{r['amount']:>8.2f} "
              f"[{r['category_code']:<7}] {r['cp'][:18]:<18} {name:<32} ← {r['_why']}")

    if args.write_sql:
        stmts = []
        for r in final:
            stmts.append(
                f"UPDATE t_transaction SET category_id = {cat_service}, category_src = 'rule'\n"
                f" WHERE trans_id = {r['trans_id']} AND category_id <> {cat_service};"
                f"   -- ¥{r['amount']:.2f} {r['cp'][:16]} ({r['_why']})"
            )
        head = f"""-- =============================================================
--  服务/充值改判  sql/07_service_out.sql
--  由 scripts/reclass_service.py --write-sql 生成，请勿手改
--
--  背景：驾校报名、话费充值这类**服务**不存在「买回来闲置」，但被规则分类器
--        按商户+关键词猜进了 STUDY / LIFE（is_durable=1），混进标注候选池。
--        候选池 24 笔大额里有 7 笔（¥1,820 / 11%）是这类。
--        SERVICE 的 is_durable=0，改判后自动掉出 v_idle_candidate。
--  本批 {len(stmts)} 笔，金额 ¥{sum(r['amount'] for r in final):,.2f}
--  幂等：WHERE 带 category_id <> {cat_service}
--  注意：只改 category_id，不动 product_desc（它是 dedup_key 的输入）
--  注意：category_src 是 ENUM(rule/manual/uncategorized)，所以填 rule
-- =============================================================

USE spending_waste;
SET NAMES utf8mb4;

"""
        out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(head + "\n".join(stmts) + "\n")
        print(f"\n已生成 {out}（{len(stmts)} 条 UPDATE）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
