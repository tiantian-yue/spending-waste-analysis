#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「浪费体检」页回传的 JSON packet 落进 t_idle_label。

数据来源：scripts/gen_result_import.py 生成的「体检结果导入」页，
用户粘贴体检页导出内容后由该页产出的 JSON。

设计要点（都踩过坑）：
1. **口径唯一**：候选池只认视图 v_idle_candidate，不自己拼 WHERE。
2. **两级精度分档**：大额笔逐笔确认（sample_stratum='big_item'），
   品类级是先验推算（sample_stratum='cat_prior'）——后者**不能当 ground truth**，
   建模时要么加权用，要么只用 big_item 层。
3. **幂等**：t_idle_label 有 uk_label_trans 唯一键，用 ON DUPLICATE KEY UPDATE，
   反复跑同一份 packet 结果一致。
4. **说不清要单独记**：不塞进 is_idle，避免污染训练集。用 note 标记，
   模型只吃明确 0/1 的样本。
5. **category_id 不写死**：一律按 category_code 查（事故教训，见 README）。
"""
import argparse
import json
import os
import sys

import pymysql

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", "123456"),
    database=os.getenv("DB_NAME", "spending_waste"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)

# 闲置程度：0 无 / 1 轻度 / 2 重度 / 3 完全浪费
# 品类级是"该类大约多少闲置"，逐笔拿不到个体程度 → 统一给 1（轻度，保守）
CAT_DEGREE = 1


def load_packet(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check(pkt, conn):
    """落库前把能查的都查一遍，任一硬性问题就退出。"""
    problems = []
    with conn.cursor() as cur:
        cur.execute("SELECT trans_id, amount FROM v_idle_candidate")
        cand = {int(r["trans_id"]): float(r["amount"]) for r in cur.fetchall()}

        # 1. packet 里的交易号必须都在候选池里
        for tid in list(pkt["big"]) + list(pkt.get("missing_big", [])):
            if int(tid) not in cand:
                problems.append(f"交易号 {tid} 不在候选池 v_idle_candidate 里")

        # 2. packet 里的品类码必须都有效
        cur.execute("SELECT category_code FROM t_category")
        codes = {r["category_code"] for r in cur.fetchall()}
        for c in pkt["cat"]:
            if c not in codes:
                problems.append(f"品类码 {c} 不存在于 t_category")

        # 3. 对账：packet 自报金额 vs 用候选池真实金额重算
        #
        # ⚠ 口径陷阱（踩过）：体检页的「品类级金额」是**该类小额笔的金额**，
        # 大额笔被单独拎出来逐笔问（否则同一笔会被算两次：品类比例 + 逐笔确认）。
        # 所以这里重算品类级时，必须把大额阈值以上的笔先排除，
        # 拿"整类金额"乘比例会虚高（实测差 ¥14,180.49，正好是大额池总额）。
        big_ids = {int(i) for i in pkt["big"]}
        thr = float(pkt.get("big_threshold", 0))

        cur.execute(
            """SELECT c.category_code,
                      COALESCE(SUM(CASE WHEN v.amount < %s THEN v.amount ELSE 0 END),0) amt_small
               FROM v_idle_candidate v JOIN t_category c ON c.category_id=v.category_id
               GROUP BY c.category_code""",
            (thr,),
        )
        cat_pool = {r["category_code"]: float(r["amt_small"]) for r in cur.fetchall()}
        calc_cat = sum(cat_pool.get(c, 0) * v for c, v in pkt["cat"].items())
        # 大额笔也可能 < 阈值（页面阈值与 packet 不一致时），统一按 cand 里的实际金额
        calc_big = sum(cand[int(i)] for i, v in pkt["big"].items() if v == 1)
        calc = round(calc_cat + calc_big, 2)
        if abs(calc - pkt["waste_mine"]) > 0.02:
            problems.append(
                f"金额对不上：packet 自称 {pkt['waste_mine']}，"
                f"按库内金额重算 {calc}（品类级 {calc_cat:.2f} + 大额级 {calc_big:.2f}）"
            )

        # 4. 不该有未填项
        if pkt.get("missing_cat"):
            problems.append(f"有品类未填：{pkt['missing_cat']}")
        if pkt.get("missing_big"):
            problems.append(f"有大额未判：{pkt['missing_big']}")
        if pkt.get("unknown"):
            problems.append(f"有解析失败条目：{pkt['unknown']}")

        # 5. 不能全是"说不清"
        vague = set(pkt.get("vague_big", []))
        if vague and len(vague) >= len(pkt["big"]):
            problems.append("所有大额都标了「说不清」，没有可用的训练标签")
    return problems, cand, cat_pool, calc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("packet", help="体检结果 JSON 文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只检查不写库")
    ap.add_argument("--force", action="store_true", help="忽略对账不一致，仍然写入")
    args = ap.parse_args()

    pkt = load_packet(args.packet)
    conn = pymysql.connect(**DB)
    try:
        problems, cand, cat_pool, calc = check(pkt, conn)
        print(f"候选池 {len(cand)} 笔，¥{sum(cand.values()):,.2f}")
        print(f"packet 自称浪费 ¥{pkt['waste_mine']:,.2f} / 按库内重算 ¥{calc:,.2f}")
        if problems:
            print("\n发现硬性问题：")
            for p in problems:
                print(f"  ✗ {p}")
            if not args.force:
                print("\n未通过检查，已终止（如确认无误可加 --force）")
                return 1

        # ---- 组装写入 ----
        vague = {int(i) for i in pkt.get("vague_big", [])}
        big_rows = []
        for tid, v in pkt["big"].items():
            tid = int(tid)
            if tid in vague:
                big_rows.append((tid, 0, 0, None, None, "big_item",
                                 "说不清，待回看（不计入正负样本）"))
            else:
                big_rows.append((
                    tid, int(v), 2 if v == 1 else 0,
                    "其他" if v == 1 else None, None, "big_item",
                    "大额逐笔确认",
                ))

        # 品类级：先验只摊到**小额笔**上（与体检页口径一致：
        # 大额笔已被逐笔确认，不能再叠加一次品类先验，否则同一笔算两遍）
        cat_rows = []
        with conn.cursor() as cur:
            for code, pct in pkt["cat"].items():
                if pct <= 0:
                    continue
                cur.execute(
                    """SELECT v.trans_id FROM v_idle_candidate v
                       JOIN t_category c ON c.category_id=v.category_id
                       WHERE c.category_code=%s AND v.amount < %s""",
                    (code, pkt.get("big_threshold", 0)),
                )
                ids = [int(r["trans_id"]) for r in cur.fetchall()]
                for tid in ids:
                    cat_rows.append((
                        tid, 0, CAT_DEGREE, None, None, "cat_prior",
                        f"品类先验 {code}={pct:.0%}（该比例用于加权，非逐笔事实）",
                    ))

        all_rows = big_rows + cat_rows
        # 去重：大额笔若同时被品类覆盖，以 big_item 为准（逐笔 > 先验）
        seen, final = set(), []
        for r in big_rows:                      # 先放大额
            seen.add(r[0]); final.append(r)
        for r in cat_rows:
            if r[0] in seen:
                continue
            seen.add(r[0]); final.append(r)

        print(f"\n准备写入 {len(final)} 行（大额 {len(big_rows)} + 品类先验 {len(cat_rows)}，"
              f"去重后 {len(final)}）")
        if args.dry_run:
            print("--dry-run，未写库")
            return 0

        sql = """INSERT INTO t_idle_label
                 (trans_id, is_idle, idle_degree, idle_reason, used_days,
                  sample_stratum, note)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)
                 ON DUPLICATE KEY UPDATE
                   is_idle=VALUES(is_idle), idle_degree=VALUES(idle_degree),
                   idle_reason=VALUES(idle_reason), used_days=VALUES(used_days),
                   sample_stratum=VALUES(sample_stratum), note=VALUES(note)"""
        with conn.cursor() as cur:
            cur.executemany(sql, final)
        conn.commit()
        print("✓ 写入完成")

        # ---- 落库后自检 ----
        with conn.cursor() as cur:
            cur.execute("SELECT sample_stratum, COUNT(*) n, SUM(is_idle) idle "
                        "FROM t_idle_label GROUP BY sample_stratum")
            print("\n落库结果：")
            for r in cur.fetchall():
                print(f"  {r['sample_stratum']:>10}  {r['n']:>4} 笔，其中闲置 {r['idle']} 笔")
            cur.execute(
                """SELECT ROUND(SUM(v.amount),2) amt
                   FROM t_idle_label l JOIN v_idle_candidate v ON v.trans_id=l.trans_id
                   WHERE l.sample_stratum='big_item' AND l.is_idle=1"""
            )
            print(f"  大额层确认浪费 ¥{cur.fetchone()['amt']:,.2f}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
