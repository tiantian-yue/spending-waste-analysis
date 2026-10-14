#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""体检结果回填 + 导出（不靠浏览器下载，改成"复制粘贴"）。

为什么要有这一页
----------------
"浪费体检"页导出的 CSV 用的是 `Blob` + `a.download`。这套在普通浏览器里能下载，
但在**内置预览面板 / iframe（无 allow-downloads）** 里会被静默拦掉——点了没反应，
不报错也不提示。用户踩了这个坑（"我点那个导出结论 CSV 怎么没有反应啊"）。

所以这一页换成最不依赖环境的方式：**从体检页复制「导出结果」，粘进这里**，
服务端写文件、落库。粘贴是最小的公共能力，不会再被沙箱拦截。

用法
----
    python scripts/gen_result_import.py --out "../体检结果导入.html"
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def q(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch(conn):
    """取一份 key：交易号 + 商品名 + 品类 + 金额，供前端做完整性核对。"""
    rows = q(conn, """
        SELECT v.trans_id AS id,
               COALESCE(v.goods_title, '') AS gt,
               v.category_code AS cc,
               v.amount AS amt
        FROM v_idle_candidate v
        ORDER BY v.amount DESC
    """)
    out = []
    for r in rows:
        out.append({"id": int(r["id"]), "gt": (r["gt"] or "")[:36],
                    "cc": r["cc"], "amt": float(r["amt"])})
    return out


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main():
    ap = argparse.ArgumentParser(description="生成体检结果导入页")
    ap.add_argument("--out", default="../体检结果导入.html")
    ap.add_argument("--big", type=float, default=100.0)
    args = ap.parse_args()

    conn = None
    try:
        from app.services.importers import pipeline
        conn = pipeline.connect()
        rows = fetch(conn)
    finally:
        if conn:
            conn.close()

    n_big = sum(1 for r in rows if r["amt"] >= args.big)
    n_small = len(rows) - n_big

    payload = {"big": args.big, "rows": rows}
    html = TEMPLATE
    html = html.replace("__ROWS_JSON__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__N_ALL__", str(len(rows)))
    html = html.replace("__N_BIG__", str(n_big))
    html = html.replace("__N_SMALL__", str(n_small))

    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"候选池 {len(rows)} 笔（大额 {n_big} / 小额 {n_small}）")
    print(f"已生成 {out}")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>体检结果导入 · 复制粘贴，不用下载</title>
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
  .wrap{max-width:940px;margin:0 auto;padding:20px 18px 80px}
  h1{font-size:20px;margin:0 0 4px}
  h2{font-size:15px;margin:20px 0 8px}
  .sub{color:var(--muted);font-size:13px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
  .lead{background:var(--gold-l);border-color:#eddfb8}
  .lead b{color:var(--gold)}
  textarea{width:100%;height:190px;border:1px solid var(--line);border-radius:8px;
    padding:10px 12px;font:12px/1.5 Consolas,Menlo,monospace;resize:vertical;background:#fff}
  button{font:inherit;color:inherit;border:1px solid var(--line);background:#fff;
    border-radius:6px;padding:7px 14px;cursor:pointer}
  button:hover{border-color:var(--blue);color:var(--blue)}
  button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
  button.primary:hover{background:#4c6d94;color:#fff}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
  .kpis{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
  .kpi{flex:1 1 150px;background:#f7f9fc;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
  .kpi .v{font-size:20px;font-weight:700;line-height:1.2}
  .kpi .k{font-size:12px;color:var(--muted)}
  .kpi.hi .v{color:var(--red)}
  .kpi.ok .v{color:var(--green)}
  .kpi.warn .v{color:var(--gold)}
  table.lay{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
  table.lay th,table.lay td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right}
  table.lay th:first-child,table.lay td:first-child{text-align:left}
  table.lay thead th{color:var(--muted);font-weight:400;font-size:12px}
  .mini{font-size:12px}
  .muted{color:var(--muted)}
  .miss{color:var(--red)}
  .ok-t{color:var(--green)}
  #out{display:none;margin-top:10px}
  #out textarea{height:260px}
  .steps{counter-reset:s;padding-left:0;margin:6px 0 0;list-style:none}
  .steps li{counter-increment:s;position:relative;padding-left:26px;margin-bottom:5px;font-size:13px}
  .steps li::before{content:counter(s);position:absolute;left:0;top:1px;width:18px;height:18px;
    border-radius:50%;background:var(--blue);color:#fff;font-size:11px;font-weight:700;
    display:flex;align-items:center;justify-content:center}
</style>
</head>
<body>
<div class="wrap">

  <h1>体检结果导入 <span class="mini muted">复制粘贴，不依赖浏览器下载</span></h1>
  <div class="sub">候选池共 __N_ALL__ 笔（大额 __N_BIG__ / 小额 __N_SMALL__）。</div>

  <div class="card lead">
    <b>为什么改成粘贴？</b> 体检页那个「导出结论 CSV」用的是浏览器下载（Blob + 下载链接）。
    这套在普通浏览器里正常，但在**内置预览面板这类沙箱 iframe 里会被静默拦掉**——
    点了没反应，不报错也不提示。粘贴是最小的公共能力，不会被拦。
    <ol class="steps" style="margin-top:10px">
      <li>回到「浪费体检」页，点 <b>导出结论 CSV</b>（会弹出一个小窗）</li>
      <li>在小窗里点 <b>复制</b>，回到这里，粘到下面第一个框</li>
      <li>点 <b>核对导入内容</b>，看有没有对不上、漏填的</li>
      <li>点 <b>生成可落库文件</b>，把第二个框里的内容复制给我</li>
    </ol>
  </div>

  <div class="card">
    <h2 style="margin-top:0">① 粘贴体检页导出的结论</h2>
    <textarea id="inp" spellcheck="false"
      placeholder="把体检页「导出结论 CSV」小窗里的内容整段粘到这里…"></textarea>
    <div class="row">
      <button class="primary" id="btn-check">核对导入内容</button>
      <button id="btn-clear">清空</button>
      <span class="mini muted" id="hint"></span>
    </div>
  </div>

  <div id="out">
    <div class="card">
      <h2 style="margin-top:0">② 核对结果</h2>
      <div class="kpis">
        <div class="kpi hi"><div class="v" id="r-waste">—</div><div class="k">预计浪费</div></div>
        <div class="kpi ok"><div class="v" id="r-keep">—</div><div class="k">没白花</div></div>
        <div class="kpi"><div class="v" id="r-cat">—</div><div class="k">品类级已回答</div></div>
        <div class="kpi"><div class="v" id="r-big">—</div><div class="k">大额已确认</div></div>
        <div class="kpi warn"><div class="v" id="r-miss">—</div><div class="k">需要补的</div></div>
      </div>
      <div id="issues" class="mini" style="margin-top:10px"></div>
    </div>

    <div class="card" id="card-big">
      <h2 style="margin-top:0">大额笔逐笔明细</h2>
      <table class="lay">
        <thead><tr><th>交易号</th><th>商品</th><th>金额</th><th>你的判定</th></tr></thead>
        <tbody id="tb-big"></tbody>
      </table>
    </div>

    <div class="card">
      <h2 style="margin-top:0">③ 把下面这段复制给我（我按这个落库）</h2>
      <textarea id="packet" spellcheck="false" readonly></textarea>
      <div class="row">
        <button class="primary" id="btn-copy">复制这一段</button>
        <span class="mini muted" id="copy-hint"></span>
      </div>
    </div>
  </div>

</div>

<script>
"use strict";
const DATA = __ROWS_JSON__;
const BIG  = DATA.big;
const ROWS = DATA.rows;

const $ = s => document.querySelector(s);
const catIdx = {};                       // 品类码 → {名称, 笔数, 金额}
const amtIdx = {};                       // 交易号 → 金额
ROWS.forEach(r => { amtIdx[r.id] = r.amt; });

// 品类汇总（由候选池自己算，不靠体检页传，避免两边口径漂移）
ROWS.forEach(r => {
  if(r.amt >= BIG) return;               // 大额单独确认，不参与品类汇总
  const c = catIdx[r.cc] || (catIdx[r.cc] = { n:0, amt:0 });
  c.n++; c.amt += r.amt;
});
const CATNAMES = {};
ROWS.forEach(r => { CATNAMES[r.cc] = CATNAMES[r.cc] || r.cc; });

function fmt(n){ return "¥" + Math.round(n).toLocaleString("en-US"); }

// ---- 解析粘贴内容（CSV，带引号转义）----
function splitCsvLine(line){
  const out = []; let cur = "", q = false;
  for(let i = 0; i < line.length; i++){
    const ch = line[i];
    if(q){
      if(ch === '"'){ if(line[i+1] === '"'){ cur += '"'; i++; } else q = false; }
      else cur += ch;
    }else{
      if(ch === '"') q = true;
      else if(ch === ","){ out.push(cur); cur = ""; }
      else cur += ch;
    }
  }
  out.push(cur);
  return out;
}

let parsed = null;

function parse(text){
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const res = { waste:null, total:null, cats:{}, bigs:{}, unknown:[] };
  let section = "";
  for(const raw of lines){
    if(!raw.trim()) continue;
    const cells = splitCsvLine(raw);

    if(cells[0] === "汇总"){
      res.waste = parseFloat(cells[2]); res.total = parseFloat(cells[4]);
      continue;
    }
    if(cells[0] === "品类级结论"){ section = "cat"; continue; }
    if(cells[0] === "大额单笔确认"){ section = "big"; continue; }
    if(section === "cat"){
      if(cells[0] === "品类码") continue;                 // 表头
      const code = cells[0];
      if(!code) continue;
      if(!catIdx[code]){ res.unknown.push(["品类不存在", code]); continue; }
      const pct = parseFloat(String(cells[4]).replace("%", "")) || 0;
      res.cats[code] = pct / 100;
      continue;
    }
    if(section === "big"){
      if(cells[0] === "交易号") continue;                 // 表头
      const id = String(cells[0]);
      if(!id) continue;
      if(amtIdx[id] === undefined){ res.unknown.push(["交易号不存在", id]); continue; }
      const v = cells[5];
      res.bigs[id] = v === "闲置" ? 1 : (v === "在用" ? 0 : (v === "说不清" ? 2 : undefined));
      continue;
    }
  }
  return res;
}

// ---- 核对 ----
$("#btn-check").onclick = () => {
  const text = $("#inp").value;
  if(!text.trim()){ $("#hint").textContent = "先粘贴体检页导出的内容"; return; }

  const p = parse(text);
  parsed = p;
  const issues = [];

  // 1. 汇总对不上
  let catWaste = 0;
  Object.keys(p.cats).forEach(c => { catWaste += catIdx[c].amt * p.cats[c]; });
  let bigWaste = 0;
  Object.keys(p.bigs).forEach(id => { if(p.bigs[id] === 1) bigWaste += amtIdx[id]; });
  const mine = catWaste + bigWaste;

  if(p.waste !== null && Math.abs(p.waste - mine) > 0.02){
    issues.push(`预计浪费对不上：你那边 ${p.waste}，我这边按同一份数据算 ${mine.toFixed(2)}`
      + `（可能是页面数据版本不同，以我这边为准）`);
  }

  // 2. 漏填的品类
  const missCat = Object.keys(catIdx).filter(c => p.cats[c] === undefined);
  const missCatAmt = missCat.reduce((a, c) => a + catIdx[c].amt, 0);
  if(missCat.length){
    issues.push(`有 ${missCat.length} 个品类没回答（涉及 ${fmt(missCatAmt)}）：
      ${missCat.join("、")} —— 这些按 0% 闲置计入`);
  }

  // 3. 漏判的大额
  const bigIds = ROWS.filter(r => r.amt >= BIG).map(r => String(r.id));
  const missBig = bigIds.filter(id => p.bigs[id] === undefined);
  const unsaid  = bigIds.filter(id => p.bigs[id] === 2);
  if(missBig.length) issues.push(`有 ${missBig.length} 笔大额没判定：${missBig.slice(0,8).join("、")}`
    + (missBig.length > 8 ? " …" : "") + " —— 按「不浪费」计入");
  if(unsaid.length) issues.push(`有 ${unsaid.length} 笔大额选了「说不清」：按「不浪费」计入，但我会标出来等你回看`);
  if(p.unknown.length) issues.push(`有 ${p.unknown.length} 条解析不了：`
    + p.unknown.slice(0,5).map(x => x.join("=")).join("、"));

  $("#r-waste").textContent = fmt(mine);
  $("#r-keep").textContent  = fmt(Math.max(0, (p.total || 0) - mine));
  $("#r-cat").textContent   = Object.keys(p.cats).length + "/" + Object.keys(catIdx).length;
  $("#r-big").textContent   = Object.keys(p.bigs).length + "/" + bigIds.length;
  $("#r-miss").textContent  = missCat.length + missBig.length + p.unknown.length;

  $("#issues").innerHTML = issues.length
    ? issues.map(t => `<div class="miss">· ${t.replace(/</g,"&lt;")}</div>`).join("")
    : `<div class="ok-t">· 全部对得上，没有漏填。</div>`;

  // 大额明细
  const LAB = {0:"在用", 1:"闲置", 2:"说不清"};
  $("#tb-big").innerHTML = ROWS.filter(r => r.amt >= BIG).map(r => {
    const id = String(r.id), v = p.bigs[id];
    return `<tr><td>#${id}</td><td style="text-align:left">${(r.gt || "（无商品名）").replace(/</g,"&lt;")}</td>`
         + `<td>${r.amt.toFixed(2)}</td>`
         + `<td class="${v===1?"miss":""}">${v === undefined ? "未判" : LAB[v]}</td></tr>`;
  }).join("");

  // 打包
  const packet = {
    source: "waste_checkup", big_threshold: BIG,
    waste_user: p.waste, waste_mine: +mine.toFixed(2), total: p.total,
    cat: p.cats, big: p.bigs,
    missing_cat: missCat,
    missing_big: missBig.map(Number),
    vague_big: unsaid.map(Number),
    unknown: p.unknown
  };
  $("#packet").value = JSON.stringify(packet, null, 1);
  $("#out").style.display = "block";
  $("#hint").textContent = "";
};

$("#btn-clear").onclick = () => {
  $("#inp").value = ""; $("#out").style.display = "none"; $("#hint").textContent = "";
};

$("#btn-copy").onclick = () => {
  const el = $("#packet");
  el.removeAttribute("readonly");
  el.select(); el.setSelectionRange(0, el.value.length);
  let ok = false;
  try{ ok = document.execCommand("copy"); }catch(e){ ok = false; }
  el.setAttribute("readonly", "readonly");
  if(!ok && navigator.clipboard){ navigator.clipboard.writeText(el.value).then(()=>{}, ()=>{}); ok = true; }
  $("#copy-hint").textContent = ok ? "已复制，粘到对话里发我即可" : "复制失败 → 手动全选（Ctrl+A）再复制";
  setTimeout(() => { $("#copy-hint").textContent = ""; }, 3000);
};
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
