# -*- coding: utf-8 -*-
"""把「拼多多订单明细」按金额匹配回账单里的「先用后付」扣款，并给出品类回填建议。

匹配为什么只能用金额 + 顺序
--------------------------
拼多多订单列表页不显示订单号、不显示下单日期；账单那边的商户字段只写
「先用后付」+「拼多多平台商户」。两边**没有任何公共键**，可用线索只有：
    ① 金额（页面「实付」= 先用后付扣款金额）
    ② 卡片上的「先用后付」标签（说明这单确实是先用后付付的）
    ③ 顺序（页面按时间倒序排列，账单按时间正序）

确定性分级
----------
    唯一   该金额只对应一件商品            → 直接定品类
    同类   多件候选但大类一致              → 按大类定品类
    对齐   候选数与交易数相等，按顺序对齐  → 逐笔定品类（页面倒序 ↔ 账单正序）
    跨类   仍无法判定                      → 输出候选供人工确认
    无     页面里找不到同金额商品          → 保持「电商综合」

用法
----
    python scripts/match_pdd_to_bills.py --orders data/pdd_classified.csv -o data/pdd_match.csv
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import os
import subprocess
import sys

MYSQL = r"F:/allstudy/mysql/server/bin/mysql.exe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def query(db: str, sql: str) -> list[list[str]]:
    r = subprocess.run([MYSQL, "-uroot", "-p123456", "--default-character-set=utf8mb4",
                        "--batch", "--raw", "-e", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("MySQL 查询失败：\n" + r.stderr)
    return [l.split("\t") for l in r.stdout.splitlines() if l.strip()][1:]


def load_transactions(db: str) -> list[dict]:
    sql = ("SELECT DATE_FORMAT(trade_time,'%Y-%m-%d %H:%i:%s'), amount, trans_id "
           f"FROM {db}.t_transaction "
           "WHERE product_desc LIKE '%%先用后付%%' AND direction='out' "
           "AND is_primary=1 AND is_refund=0 ORDER BY trade_time")
    return [{"date": d, "amount": float(a), "id": i} for d, a, i in query(db, sql)]


def load_orders(path: str) -> list[dict]:
    rows = [r for r in csv.DictReader(io.open(path, encoding="utf-8-sig")) if not r["退款"]]
    for r in rows:
        r["_seq"] = int(r.get("次序") or 0)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="spending_waste")
    ap.add_argument("--orders", default=os.path.join(ROOT, "data", "pdd_classified.csv"))
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "data", "pdd_match.csv"))
    args = ap.parse_args()

    txs = load_transactions(args.db)
    items = load_orders(args.orders)
    by_pay: dict[str, list[dict]] = collections.defaultdict(list)
    by_all: dict[str, list[dict]] = collections.defaultdict(list)
    for it in items:
        by_all[it["实付"]].append(it)
        if it.get("支付方式") == "先用后付":
            by_pay[it["实付"]].append(it)
    print(f"先用后付交易 {len(txs)} 笔 · 页面有效商品行 {len(items)} 条"
          f"（其中带「先用后付」标签 "
          f"{sum(len(v) for v in by_pay.values())} 条）")

    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for t in txs:
        groups[f'{t["amount"]:.2f}'].append(t)

    stat = collections.Counter()
    rows_out = []
    for key, ts in sorted(groups.items()):
        cand = by_pay.get(key) or by_all.get(key) or []
        cand = sorted(cand, key=lambda c: c["_seq"])        # 页面顺序：新 → 旧
        cats = {c["大类"] for c in cand}
        assign: dict[str, dict] = {}
        if not cand:
            level = "无"
        elif len(cand) == 1:
            level = "唯一"
            assign = {t["id"]: cand[0] for t in ts}
        elif len(cats) == 1:
            level = "同类"
            assign = {t["id"]: cand[0] for t in ts}
        elif len(cand) == len(ts):
            level = "对齐"
            for t, c in zip(reversed(ts), cand):            # 最新 ↔ 最新
                assign[t["id"]] = c
        else:
            level = "跨类"
            # 取候选里的主导类作为建议值，但保留「跨类」标记供人工复核
            top = collections.Counter(c["大类"] for c in cand).most_common()
            best = max(top, key=lambda x: (x[1], -len(x[0])))
            code = next(c["项目分类码"] for c in cand if c["大类"] == best[0])
            for t in ts:
                stat[level] += 1
                desc = " ;; ".join(f'{x["店铺"]}·{x["商品标题"]}[{x["大类"]}]' for x in cand[:4])
                if len(cand) > 4:
                    desc += f' …(共{len(cand)}条)'
                rows_out.append([t["date"], key, len(cand), level, best[0], code, "", desc])
            continue
        for t in ts:
            stat[level] += 1
            c = assign.get(t["id"])
            desc = " ;; ".join(f'{x["店铺"]}·{x["商品标题"]}[{x["大类"]}]' for x in cand[:4])
            if len(cand) > 4:
                desc += f' …(共{len(cand)}条)'
            rows_out.append([t["date"], key, len(cand), level,
                             c["大类"] if c else "", c["项目分类码"] if c else "",
                             c["商品标题"] if c else "", desc])

    with io.open(args.out, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["交易时间", "金额", "候选数", "确定性", "建议大类", "项目分类码",
                    "匹配商品（判定用）", "全部候选"])
        w.writerows(rows_out)

    n = len(txs)
    print("\n=== 品类回填确定性 ===")
    for lv in ("唯一", "同类", "对齐", "跨类", "无"):
        if stat[lv]:
            print(f"   {lv}: {stat[lv]:>4} 笔  {100*stat[lv]/n:5.1f}%")
    ok = stat["唯一"] + stat["同类"] + stat["对齐"]
    print(f"\n可直接定品类：{ok} 笔 = {100*ok/n:.1f}%（+¥"
          f"{sum(float(r[1]) for r in rows_out if r[3] in ('唯一','同类','对齐')):.2f}）")
    print(f"需人工确认：{stat['跨类']} 笔 = {100*stat['跨类']/n:.1f}%")
    print(f"\n输出 → {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
