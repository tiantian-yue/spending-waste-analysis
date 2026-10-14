#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""三分钟浪费体检：把「逐笔盘点」换成「按类回答」。

设计动机
--------
`gen_label_workbench.py` 产出的逐笔盘点表，本质是**给研究者自己造训练集**的
工具，不是产品功能。它的前提是"标注者愿意为每一笔回忆、填 5 个字段"——
换任何一个人来用都不成立，所以没有普适性。

而"这件东西闲置了没"这个信息，**账单里一个字都没有**（账单只记到收单机构），
它只存在于人的事后记忆里。所以任何方案都绕不开人的输入，问题只是：
把人的输入单位从「笔」换成「类」。

人工量对比（本次数据）
  逐笔盘点：705 笔 × 5 个字段 ≈ 3,500 次输入
  本次体检：6 个品类 × 1 次 + 18 笔大额 × 1 次 = 24 次点击

代价
----
品类内的差异被平均掉（同类里有的在用有的闲置，最后只落一个比例）。
换来的是可用性，以及**可迁移性**：换一份别人的账单，同样只需要回答
几个品类问题 + 确认他自己的大额笔。

三层漏斗（能被规则排除的，绝不让人看）
  L1 规则硬排除（0 人工）：食品生鲜 / 服务充值 → 见 reclass_food_as_groc.py、
     reclass_service.py，靠 is_durable=0 自动掉出候选池
  L2 品类级判断（6 次点击）：6 个品类的整体闲置倾向
  L3 单品级确认（18 次点击）：≥¥100 的笔单独问，因为钱高度集中在这里

用法
----
    python scripts/gen_waste_checkup.py --out "../浪费体检.html"
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.services.importers import pipeline  # noqa: E402

BIG = 100.0            # 大额阈值：>=100 元单独逐笔确认
ANCHOR_N = 6           # 每个品类展示几个真实商品名锚点
ANCHOR_LEN = 20        # 锚点截断长度（太长会挤成一片，看不清"这类"是什么）

# 档位：值 → (标签, 说明)
LEVELS = [
    ("0.1", "基本都在用", "买了就用了，只有个别闲置"),
    ("0.3", "少部分闲置", "大多数在用，少数吃灰"),
    ("0.5", "约一半闲置", "一半用一半放着"),
    ("0.8", "大多闲置", "买了基本没怎么用"),
]


def q(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch(conn):
    """注意：无参数 execute() 时 pymysql 不还原 %%，这里必须写单 %。"""
    cats = q(conn, """
        SELECT v.category_code, v.category_name,
               COUNT(*) AS n, ROUND(SUM(v.amount),2) AS amt,
               SUM(v.amount >= %d) AS n_big,
               ROUND(SUM(CASE WHEN v.amount >= %d THEN v.amount ELSE 0 END),2) AS amt_big
        FROM v_idle_candidate v
        GROUP BY v.category_code, v.category_name
        ORDER BY amt DESC
    """ % (BIG, BIG))

    anchors = q(conn, """
        SELECT v.category_code, v.goods_title, v.amount
        FROM v_idle_candidate v
        WHERE v.goods_title IS NOT NULL AND v.amount < %d
        ORDER BY v.category_code, v.amount DESC
    """ % BIG)

    bigs = q(conn, """
        SELECT v.trans_id, DATE_FORMAT(v.trade_time,'%%Y-%%m-%%d') AS tm,
               v.category_code, v.category_name, v.counterparty,
               v.product_desc, v.goods_title, v.alt_titles, v.amount
        FROM v_idle_candidate v
        WHERE v.amount >= %d
        ORDER BY v.amount DESC
    """ % BIG)

    for a in cats:
        a["amt"] = float(a["amt"])
        a["amt_big"] = float(a["amt_big"] or 0)
        a["amt_small"] = round(a["amt"] - a["amt_big"], 2)
        a["n_small"] = int(a["n"] - a["n_big"])
    for b in bigs:
        b["amount"] = float(b["amount"])
    return cats, anchors, bigs


def build_anchors(anchors, valid_codes):
    """每类取 N 个商品名锚点。

    两点讲究：
      1. **前缀去重** —— 同款商品标题前缀几乎一样（"羊羔绒阔腿裤子女秋冬…" vs
         "羊羔绒阔腿裤女秋冬…"），只按金额取前 N 会拿到一堆同款，看着像只有一类东西。
         按前 4 字去重就能自然打散。
      2. **截断** —— 拼多多标题动辄 40 字，全铺出来是一堵墙，用户反而看不出"这类
         东西"是什么。截到 ANCHOR_LEN 字，够认出来就行。
    输入已按 (category_code, amount DESC) 排好序。
    """
    out = {c: [] for c in valid_codes}
    seen = {c: set() for c in valid_codes}
    for a in anchors:
        c = a["category_code"]
        if c not in out or len(out[c]) >= ANCHOR_N:
            continue
        t = (a["goods_title"] or "").strip()
        if not t:
            continue
        prefix = t[:4]
        if prefix in seen[c]:
            continue
        seen[c].add(prefix)
        out[c].append(t[:ANCHOR_LEN] + ("…" if len(t) > ANCHOR_LEN else ""))
    return out


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main():
    ap = argparse.ArgumentParser(description="生成品类级浪费体检页")
    ap.add_argument("--out", default="../浪费体检.html")
    args = ap.parse_args()

    conn = pipeline.connect()
    cats, anchors, bigs = fetch(conn)
    conn.close()

    codes = [c["category_code"] for c in cats]
    anc = build_anchors(anchors, codes)

    total_n = sum(c["n"] for c in cats)
    total_amt = round(sum(c["amt"] for c in cats), 2)
    big_amt = round(sum(b["amount"] for b in bigs), 2)
    small_amt = round(total_amt - big_amt, 2)
    small_n = total_n - len(bigs)

    # -------- 品类卡 --------
    cat_cards = []
    for c in cats:
        chips = "".join(f'<span class="chip">{esc(t)}</span>' for t in anc[c["category_code"]])
        btns = "".join(
            f'<button class="lv" data-cat="{c["category_code"]}" data-pct="{v}">'
            f'<b>{lab}</b><i>{desc}</i></button>'
            for v, lab, desc in LEVELS)
        cat_cards.append(f'''
  <div class="card cat" data-cat="{c["category_code"]}">
    <div class="chead">
      <div>
        <span class="cname">{esc(c["category_name"])}</span>
        <span class="tag gray">{esc(c["category_code"])}</span>
      </div>
      <div class="cnum">
        <b>{c["n_small"]}</b> 笔 · <b>¥{c["amt_small"]:,.0f}</b>
      </div>
    </div>
    <div class="cdesc">共 {c["n"]} 笔 ¥{c["amt"]:,.0f}，其中 ≥¥{BIG:.0f} 的 {int(c["n_big"])} 笔
      （¥{c["amt_big"]:,.0f}）已单独列出，这里只管剩下的小额中额。</div>
    <div class="chips">{chips}</div>
    <div class="lvrow">{btns}</div>
    <div class="cres">这一类的闲置比例：<b class="pick">未选</b>
      ｜ 对应浪费 <b class="w-amt">—</b></div>
  </div>''')

    # -------- 大额逐笔 --------
    big_rows = []
    for b in bigs:
        name = (b["goods_title"] or "").strip()
        note = ""
        if not name:
            alt = (b["alt_titles"] or "").strip()
            if alt:
                name, note = alt.split(" / ")[0], "（同金额多件，取第一个候选）"
            else:
                name, note = (b["product_desc"] or "").strip(), "（账单没记商品名）"
        big_rows.append(f'''
      <tr data-id="{b["trans_id"]}">
        <td class="mini muted">{esc(b["tm"])}</td>
        <td class="mini"><span class="tag gray">{esc(b["category_name"])}</span></td>
        <td class="mini">{esc(b["counterparty"][:16])}</td>
        <td>{esc(name[:44])}<span class="mini muted">{esc(note)}</span></td>
        <td class="num"><b>¥{b["amount"]:,.2f}</b></td>
        <td class="acts">
          <button data-v="0" data-id="{b["trans_id"]}">在用</button>
          <button data-v="1" data-id="{b["trans_id"]}">闲置</button>
          <button data-v="2" data-id="{b["trans_id"]}">说不清</button>
        </td>
      </tr>''')

    meta = {
        "total_n": total_n, "total_amt": total_amt,
        "big_amt": big_amt, "small_amt": small_amt,
        "cat": {c["category_code"]: {"name": c["category_name"],
                                     "n": c["n_small"],
                                     "amt": c["amt_small"]} for c in cats},
        "big": {str(b["trans_id"]): {"amt": b["amount"],
                                     "name": (b["goods_title"] or b["product_desc"] or "")[:40],
                                     "tm": b["tm"], "cat": b["category_name"]}
                for b in bigs},
    }

    html = TEMPLATE
    html = html.replace("__META__", json.dumps(meta, ensure_ascii=False))
    html = html.replace("__CATCARDS__", "".join(cat_cards))
    html = html.replace("__BIGROWS__", "".join(big_rows))
    html = html.replace("__TOTAL_N__", str(total_n))
    html = html.replace("__TOTAL_AMT__", f"{total_amt:,.0f}")
    html = html.replace("__BIG_AMT__", f"{big_amt:,.0f}")
    html = html.replace("__BIG_N__", str(len(bigs)))
    html = html.replace("__SMALL_AMT__", f"{small_amt:,.0f}")
    html = html.replace("__SMALL_N__", str(small_n))
    html = html.replace("__NCAT__", str(len(cats)))
    html = html.replace("__BIG_PCT__", f"{big_amt / total_amt * 100:.0f}")
    html = html.replace("__CLICKS__", str(len(cats) + len(bigs)))
    html = html.replace("__INPUTS__", f"{total_n * 5 / 1000:.1f}k")

    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"品类 {len(cats)} 个 · 大额笔 {len(bigs)} 笔（≥¥{BIG:.0f}）")
    print(f"候选池 {total_n} 笔 ¥{total_amt:,.2f}"
          f"（大额 ¥{big_amt:,.2f} / 小额 ¥{small_amt:,.2f}）")
    print(f"人工量：{len(cats) + len(bigs)} 次点击（逐笔盘点约 {total_n * 5:,} 次输入）")
    print(f"已生成 {out}")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>浪费体检 · 按类回答，不用逐笔盘点</title>
<style>
  :root{
    --bg:#eef1f5; --card:#fbfcfd; --line:#d8dee6; --ink:#1d2733; --muted:#6b7785;
    --blue:#5b7ea8; --blue-l:#eef4fc; --gold:#b9892f; --gold-l:#fdf8ea;
    --red:#b4453a; --red-l:#fdf1ef; --green:#3f7d5a; --green-l:#f0f9f4;
    --gray:#8d99a6; --gray-l:#f2f4f7;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.55 "Microsoft YaHei","PingFang SC","Segoe UI",sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:20px 18px 120px}
  h1{font-size:20px;margin:0 0 4px}
  h2{font-size:15px;margin:22px 0 10px}
  .sub{color:var(--muted);font-size:13px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
  .lead{background:var(--gold-l);border-color:#eddfb8}
  .lead b{color:var(--gold)}
  .lead .grid{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}
  .lead .grid>div{flex:1 1 210px;font-size:13px}
  .cmp{font-size:12px;color:var(--muted);margin-top:8px}

  /* 实时结论条 */
  .sticky{position:sticky;top:0;z-index:20;background:var(--card);border:1px solid var(--line);
          border-radius:10px;padding:12px 16px;margin-bottom:14px;box-shadow:0 2px 10px rgba(29,39,51,.06)}
  .kpis{display:flex;gap:10px;flex-wrap:wrap}
  .kpi{flex:1 1 130px}
  .kpi .v{font-size:22px;font-weight:700;line-height:1.2}
  .kpi .k{font-size:12px;color:var(--muted)}
  .kpi.hi .v{color:var(--red)}
  .kpi.ok .v{color:var(--green)}
  .bar{height:8px;background:#e3e8ef;border-radius:99px;overflow:hidden;margin-top:10px}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--red),#d0887f);width:0;transition:width .25s}
  .tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}

  /* 品类卡 */
  .cat .chead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
  .cname{font-size:16px;font-weight:700}
  .cnum{color:var(--muted);font-size:13px}
  .cnum b{color:var(--ink);font-size:15px}
  .cdesc{color:var(--muted);font-size:12px;margin-top:4px}
  .chips{margin:9px 0 10px;display:flex;gap:6px;flex-wrap:wrap}
  .chip{background:#f1f4f8;border:1px solid var(--line);border-radius:99px;
        padding:2px 9px;font-size:12px;color:#46525f}
  .lvrow{display:flex;gap:8px;flex-wrap:wrap}
  .lv{flex:1 1 150px;text-align:left;border:1px solid var(--line);background:#fff;
      border-radius:8px;padding:8px 10px;cursor:pointer;line-height:1.35}
  .lv b{display:block;font-size:13px}
  .lv i{font-style:normal;font-size:11px;color:var(--muted)}
  .lv:hover{border-color:var(--blue)}
  .lv.on{background:var(--blue-l);border-color:var(--blue)}
  .lv.on b{color:var(--blue)}
  .cres{margin-top:9px;font-size:12px;color:var(--muted);border-top:1px dashed var(--line);padding-top:8px}
  .cres b{color:var(--ink)}
  .cres .w-amt{color:var(--red)}

  table.lay{width:100%;border-collapse:collapse;font-size:13px}
  table.lay th,table.lay td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:right;vertical-align:middle}
  table.lay th:first-child,table.lay td:first-child,
  table.lay th:nth-child(4),table.lay td:nth-child(4){text-align:left}
  table.lay thead th{color:var(--muted);font-weight:400;font-size:12px}
  .mini{font-size:12px}
  .muted{color:var(--muted)}
  .num b{font-size:14px}
  .tag{display:inline-block;border-radius:5px;padding:1px 7px;font-size:11px;
       border:1px solid var(--line);background:var(--gray-l);color:#5b6672}
  .acts{white-space:nowrap}
  .acts button{border:1px solid var(--line);background:#fff;border-radius:6px;
               padding:4px 10px;cursor:pointer;font-size:12px;margin-left:4px}
  .acts button:hover{border-color:var(--blue);color:var(--blue)}
  .acts button.on[data-v="0"]{background:var(--green-l);border-color:var(--green);color:var(--green)}
  .acts button.on[data-v="1"]{background:var(--red-l);border-color:var(--red);color:var(--red)}
  .acts button.on[data-v="2"]{background:var(--gray-l);border-color:var(--gray);color:var(--gray)}
  tr.answered{background:#fafbfc}
  tr.answered td:nth-child(4){color:var(--muted)}
  button{font:inherit;color:inherit}
  button.primary{background:var(--blue);border:1px solid var(--blue);color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer}
  button.primary:hover{background:#4c6d94}
  button.ghost{border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 12px;cursor:pointer}
  button.ghost:hover{border-color:var(--blue);color:var(--blue)}
  button.warn{border-color:#e3c9c5;color:var(--red)}
  #dlg{border:none;border-radius:10px;padding:0;max-width:820px;width:92%}
  #dlg::backdrop{background:rgba(29,39,51,.35)}
  #dlg .dh{padding:14px 16px 0;font-weight:700}
  #dlg textarea{width:100%;height:340px;border:none;border-top:1px solid var(--line);
    border-bottom:1px solid var(--line);padding:12px 16px;font:12px/1.5 Consolas,Menlo,monospace;resize:vertical}
  #dlg .df{padding:10px 16px 14px;display:flex;gap:8px;justify-content:flex-end}
</style>
</head>
<body>
<div class="wrap">

  <h1>浪费体检 <span class="tag">按类回答 · 不用逐笔盘点</span></h1>
  <div class="sub">候选池 __TOTAL_N__ 笔 ¥__TOTAL_AMT__ ，只需 <b>__CLICKS__ 次点击</b>出结论。</div>

  <div class="card lead">
    <div><b>为什么不再逐笔？</b>「这件东西闲置了没」——账单里一个字都没记。
      微信/支付宝只记到收单机构（对方=「拼多多平台商户」、商品说明=「先用后付」），
      所以这件事**只能靠你事后回忆**，任何工具都绕不开人的输入。</div>
    <div class="grid">
      <div><b>逐笔盘点</b><br>__TOTAL_N__ 笔 × 5 个字段 ≈ <b>__INPUTS__ 次输入</b>，
        而且很大一部分是在给自动分类的错误擦屁股。</div>
      <div><b>本次体检</b><br>__NCAT__ 个品类各点一下（回答「这类东西我大概闲置多少」）
        + __BIG_N__ 笔大额单笔确认 = <b>__CLICKS__ 次点击</b>。</div>
    </div>
    <div class="cmp">代价说清楚：品类内的差异被平均掉（同类里有的在用有的闲置，最后只落一个比例）。
      换来的是 <b>可迁移</b>——换一份别人的账单，同样只需回答这几个问题 + 确认他自己的大额笔。</div>
  </div>

  <div class="sticky">
    <div class="kpis">
      <div class="kpi hi"><div class="v" id="k-waste">¥0</div><div class="k">预计浪费金额</div></div>
      <div class="kpi"><div class="v" id="k-rate">0%</div><div class="k">占候选池比例</div></div>
      <div class="kpi ok"><div class="v" id="k-keep">¥__TOTAL_AMT__</div><div class="k">没白花的钱</div></div>
      <div class="kpi"><div class="v" id="k-prog">0/__CLICKS__</div><div class="k">已回应项</div></div>
    </div>
    <div class="bar"><i id="k-bar"></i></div>
    <div class="tools">
      <button class="ghost" id="btn-all-01">一键：全部基本在用</button>
      <button class="ghost" id="btn-all-03">一键：全部少部分闲置</button>
      <button class="ghost" id="btn-all-05">一键：全部约一半闲置</button>
      <span style="flex:1"></span>
      <button class="primary" id="btn-csv">导出结论 CSV</button>
      <button class="ghost" id="btn-sql">导出回填 SQL</button>
      <button class="ghost warn" id="btn-reset">清空重来</button>
    </div>
  </div>

  <h2>第一步 · 这几类东西，你买回来大概有多少是闲置的？</h2>
  <div class="sub" style="margin-bottom:10px">
    每张卡下面有几个<b>真实商品名</b>（从你账单里还原出来的），瞄一眼就知道"这类东西"指什么。
    大额笔已经单独拿走了，这里只管日常的小额中额。
  </div>
  <div id="cats">__CATCARDS__
  </div>

  <h2>第二步 · 这 __BIG_N__ 笔大额（每笔 ≥¥100，合计 ¥__BIG_AMT__，占候选池 __BIG_PCT__%），逐笔确认</h2>
  <div class="sub" style="margin-bottom:10px">
    钱高度集中在这里，所以这几笔值得单独看。「说不清」= 暂时按不浪费算，但会标出来待查。
  </div>
  <div class="card" style="padding:0">
    <table class="lay">
      <thead><tr>
        <th style="width:96px">日期</th><th style="width:92px">品类</th><th style="width:110px">商户</th>
        <th>商品</th><th style="width:92px">金额</th><th style="width:200px">用了没</th>
      </tr></thead>
      <tbody id="tb">__BIGROWS__
      </tbody>
    </table>
  </div>

</div>

<dialog id="dlg">
  <div class="dh" id="dlg-t">导出</div>
  <textarea id="dlg-body" spellcheck="false"></textarea>
  <div class="df">
    <button class="ghost" id="dlg-copy">复制</button>
    <button class="primary" id="dlg-close">关闭</button>
  </div>
</dialog>

<script>
"use strict";
const META = __META__;
const KEY  = "waste_checkup_v1";
const LEVELV = ["0.1","0.3","0.5","0.8"];

const $  = s => document.querySelector(s);
function $$(s, root){ return Array.prototype.slice.call((root || document).querySelectorAll(s)); }
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function fmt(n){ return "¥" + Math.round(n).toLocaleString("en-US"); }

// ---------------- 状态 ----------------
// 结构：{ cat:{品类码:比例}, big:{交易号:0在用|1闲置|2说不清} }
let state = { cat:{}, big:{} };

function load(){
  let raw = null;
  try{ raw = JSON.parse(localStorage.getItem(KEY) || "{}"); }catch(e){ raw = {}; }
  if(!raw || typeof raw !== "object") raw = {};
  const cat = {}, big = {};
  Object.keys(raw.cat || {}).forEach(k => {
    if(k === "undefined" || k === "null" || k === "") return;       // 清脏键
    const v = raw.cat[k];
    if(LEVELV.indexOf(String(v)) >= 0 && META.cat[k]) cat[k] = String(v);
  });
  Object.keys(raw.big || {}).forEach(k => {
    if(k === "undefined" || k === "null" || k === "") return;
    const v = Number(raw.big[k]);
    if((v === 0 || v === 1 || v === 2) && META.big[k]) big[k] = v;
  });
  state = { cat: cat, big: big };
}
function save(){
  try{ localStorage.setItem(KEY, JSON.stringify(state)); }catch(e){}
}

// ---------------- 计算 ----------------
function calc(){
  const catAmt = {}, bigAmt = {};
  Object.keys(META.cat).forEach(c => { catAmt[c] = META.cat[c].amt; });
  Object.keys(META.big).forEach(i => { bigAmt[i] = META.big[i].amt; });

  let waste = 0, catWaste = 0, bigWaste = 0;
  Object.keys(catAmt).forEach(c => { catWaste += catAmt[c] * (parseFloat(state.cat[c]) || 0); });
  Object.keys(bigAmt).forEach(i => { if(state.big[i] === 1) bigWaste += bigAmt[i]; });
  waste = catWaste + bigWaste;

  const total = META.total_amt;
  const answered = Object.keys(state.cat).length + Object.keys(state.big).length;
  const totalItems = Object.keys(META.cat).length + Object.keys(META.big).length;
  return { waste: waste, catWaste: catWaste, bigWaste: bigWaste, total: total,
           answered: answered, totalItems: totalItems };
}

function refresh(){
  const r = calc();
  const rate = r.total ? (r.waste / r.total * 100) : 0;
  $("#k-waste").textContent = fmt(r.waste);
  $("#k-rate").textContent  = rate.toFixed(1) + "%";
  $("#k-keep").textContent  = fmt(Math.max(0, r.total - r.waste));
  $("#k-prog").textContent  = r.answered + "/" + r.totalItems;
  $("#k-bar").style.width   = rate.toFixed(1) + "%";

  // 品类卡：按钮高亮 + 单类结果
  $$(".cat").forEach(card => {
    const code = card.getAttribute("data-cat");
    const pct  = state.cat[code];
    $$(".lv", card).forEach(b => b.classList.toggle("on", b.getAttribute("data-pct") === pct));
    const info = META.cat[code];
    const w = info.amt * (parseFloat(pct) || 0);
    const pick = card.querySelector(".pick");
    const wAmt = card.querySelector(".w-amt");
    if(pick) pick.textContent = pct ? (parseFloat(pct) * 100).toFixed(0) + "%" : "未选";
    if(wAmt) wAmt.textContent = pct ? fmt(w) : "—";
  });

  // 大额行
  $$("#tb tr").forEach(tr => {
    const id = tr.getAttribute("data-id");
    const v  = state.big[id];
    $$("button", tr).forEach(b => b.classList.toggle("on", String(v) === b.getAttribute("data-v")));
    tr.classList.toggle("answered", v === 0 || v === 1 || v === 2);
  });
}

// ---------------- 交互 ----------------
function autoFill(pct){
  Object.keys(META.cat).forEach(c => { state.cat[c] = pct; });
  save(); refresh();
}

document.addEventListener("click", ev => {
  const t = ev.target.closest ? ev.target.closest("button") : null;
  if(!t) return;

  if(t.classList.contains("lv")){
    const code = t.getAttribute("data-cat");
    if(!META.cat[code]) throw new Error("未知品类：" + code);
    state.cat[code] = t.getAttribute("data-pct");
    save(); refresh(); return;
  }
  if(t.hasAttribute("data-id") && t.closest("#tb")){
    const id = t.getAttribute("data-id");
    if(!META.big[id]) throw new Error("未知交易号：" + id);
    const v = Number(t.getAttribute("data-v"));
    state.big[id] = v;
    save(); refresh(); return;
  }
});

$("#btn-all-01").onclick = () => autoFill("0.1");
$("#btn-all-03").onclick = () => autoFill("0.3");
$("#btn-all-05").onclick = () => autoFill("0.5");
$("#btn-reset").onclick = () => {
  if(!confirm("清空所有选择、从头开始？")) return;
  state = { cat:{}, big:{} }; save(); refresh();
};

// ---------------- 导出 ----------------
function csvCell(v){
  v = String(v == null ? "" : v);
  return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}
function download(name, text){
  const blob = new Blob(["\ufeff" + text], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

$("#btn-csv").onclick = () => {
  const r = calc();
  const L = [];
  L.push(["汇总", "预计浪费", r.waste.toFixed(2), "候选池总额", r.total.toFixed(2)].join(","));
  L.push("");
  L.push(["品类级结论"].join(","));
  L.push(["品类码","品类","笔数","金额","闲置比例","对应浪费"].join(","));
  Object.keys(META.cat).forEach(c => {
    const i = META.cat[c], p = parseFloat(state.cat[c]) || 0;
    L.push([c, i.name, i.n, i.amt.toFixed(2), (p*100).toFixed(0)+"%",
            (i.amt*p).toFixed(2)].map(csvCell).join(","));
  });
  L.push("");
  L.push(["大额单笔确认"].join(","));
  L.push(["交易号","日期","品类","商品","金额","判定"].join(","));
  const LAB = {0:"在用", 1:"闲置", 2:"说不清"};
  Object.keys(META.big).forEach(id => {
    const b = META.big[id], v = state.big[id];
    L.push([id, b.tm, b.cat, b.name, b.amt.toFixed(2),
            (v === undefined ? "未判" : LAB[v])].map(csvCell).join(","));
  });
  download("浪费体检结论.csv", L.join("\r\n"));
};

$("#btn-sql").onclick = () => {
  const r = calc();
  const L = [];
  L.push("-- 浪费体检回填  " + new Date().toLocaleString("zh-CN"));
  L.push("-- 来源说明：品类级为「先验推算」，大额笔为「逐笔确认」，两者精度不同，");
  L.push("--           sample_stratum 已区分（cat_prior / big_item），note 里也写了。");
  L.push("USE spending_waste;");
  L.push("SET NAMES utf8mb4;");
  L.push("");
  L.push("-- ① 大额笔：逐笔确认，直接写标签");
  Object.keys(META.big).forEach(id => {
    const b = META.big[id], v = state.big[id];
    if(v === undefined || v === 2) return;
    const note = (v === 1 ? "大额单笔确认：闲置" : "大额单笔确认：在用")
               + "｜" + b.name.replace(/'/g, "''").slice(0, 40);
    L.push("INSERT INTO t_idle_label (trans_id,is_idle,idle_degree,idle_reason,used_days,sample_stratum,note)");
    L.push(" VALUES (" + id + "," + v + ",0,NULL,NULL,'big_item','" + note + "')");
    L.push(" ON DUPLICATE KEY UPDATE is_idle=VALUES(is_idle), sample_stratum=VALUES(sample_stratum), note=VALUES(note);");
  });
  L.push("");
  L.push("-- ② 品类级：按比例推算，用固定种子 20261014 抽样赋标签");
  L.push("--    注意：这是估计值不是实测值，不能当 ground truth 用；");
  L.push("--    要精确标签请在闲置盘点工作台里逐笔确认。");
  Object.keys(META.cat).forEach(c => {
    const p = parseFloat(state.cat[c]) || 0;
    if(p <= 0){ L.push("-- " + c + " 闲置比例 0%，无需回填；"); return; }
    const pct = (p * 100).toFixed(0);
    L.push("INSERT INTO t_idle_label (trans_id,is_idle,idle_degree,idle_reason,used_days,sample_stratum,note)");
    L.push("SELECT v.trans_id, IF(RAND(20261014) < " + p.toFixed(2) + ", 1, 0), 0, NULL, NULL, 'cat_prior',");
    L.push("       CONCAT('品类先验推算：', '" + c + "', ' 整体闲置约 " + pct + "%')");
    L.push("  FROM v_idle_candidate v");
    L.push(" WHERE v.category_code = '" + c + "' AND v.amount < 100");
    L.push("ON DUPLICATE KEY UPDATE note=VALUES(note);");
  });
  L.push("");
  L.push("-- 合计预计浪费 ¥" + r.waste.toFixed(2) + " / 候选池 ¥" + r.total.toFixed(2));
  $("#dlg-t").textContent = "回填 SQL（" + r.answered + "/" + r.totalItems + " 项已回应）";
  $("#dlg-body").value = L.join("\n");
  $("#dlg").showModal();
};

$("#dlg-copy").onclick = () => {
  const el = $("#dlg-body");
  el.select();
  try{ document.execCommand("copy"); $("#dlg-copy").textContent = "已复制"; }
  catch(e){ $("#dlg-copy").textContent = "复制失败"; }
  setTimeout(() => { $("#dlg-copy").textContent = "复制"; }, 1500);
};
$("#dlg-close").onclick = () => $("#dlg").close();

// ---------------- 启动 ----------------
load();
refresh();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
