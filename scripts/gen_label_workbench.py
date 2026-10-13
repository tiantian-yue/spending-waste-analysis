"""生成「闲置盘点工作台」单文件 HTML（分层抽样版）。

为什么要这个脚本而不是手工导出 Excel
--------------------------------------
低于 20 元的小额消费有 900 多笔（占实物金额 26%），逐笔标注不现实
（¥2.7 一单根本回忆不起来买的是什么），但直接丢掉又会让闲置率有偏。
所以采用**分层抽样 + 事后加权**：

    全样本层 all      —— >= 20 元，全部标注（201 笔，占实物金额 74%）
    抽样层  s1_0_5    —— 0~5 元，随机抽 N 笔
    抽样层  s2_5_10   —— 5~10 元，随机抽 N 笔
    抽样层  s3_10_20  —— 10~20 元，随机抽 N 笔

抽样必须**可复现**，否则论文里的数字无法被复核 —— 这就是本脚本存在的理由：
固定随机种子 + 只从 trans_id 排序后的列表里抽，任何人重跑都得到同一批样本。
抽样层随 SQL 一起写回 t_idle_label.sample_stratum，加权还原见
sql/01_schema.sql 里的 v_stratum_weight / v_category_idle_rate_weighted。

用法
----
    python scripts/gen_label_workbench.py                 # 默认种子 20261012，每层 60 笔
    python scripts/gen_label_workbench.py --per-stratum 80
    python scripts/gen_label_workbench.py --out ../闲置盘点工作台.html

环境变量：DB_USER / DB_PASSWORD / DB_HOST / DB_PORT / DB_NAME
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.importers import pipeline                        # noqa: E402

DEFAULT_SEED = 20261012          # 与示例数据生成器同源，便于记忆
DEFAULT_PER_STRATUM = 60
SAMPLE_STRATA = ("s1_0_5", "s2_5_10", "s3_10_20")


# -------------------------------------------------------------
# 一、取数 + 抽样
# -------------------------------------------------------------
def fetch_candidates(conn) -> list[dict]:
    """从 v_idle_candidate 取标注候选全集（口径唯一来源，见 01_schema.sql）。"""
    sql = """
        SELECT v.trans_id, DATE_FORMAT(v.trade_time, '%%Y-%%m-%%d %%H:%%i:%%s') AS tm,
               COALESCE(v.counterparty, '') AS cp,
               COALESCE(v.product_desc, '') AS pd,
               v.amount, v.category_code, v.category_name, v.stratum
        FROM v_idle_candidate v
        ORDER BY v.trade_time
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["amount"] = float(d["amount"])      # DECIMAL → float，否则 json 序列化报错
            out.append(d)
        return out


def fetch_categories(conn) -> list[dict]:
    sql = ("SELECT category_code, category_name, is_durable FROM t_category "
           "ORDER BY is_durable DESC, sort_no")
    with conn.cursor() as cur:
        cur.execute(sql)
        return [{"code": r[0], "name": r[1], "durable": int(r[2])} for r in cur.fetchall()]


def fetch_stratum_stats(conn) -> dict:
    """各抽样层的总体笔数与金额（用于工作台里那张抽样设计表）。"""
    sql = ("SELECT stratum, COUNT(*), ROUND(SUM(amount), 2) "
           "FROM v_idle_candidate GROUP BY stratum")
    with conn.cursor() as cur:
        cur.execute(sql)
        return {r[0]: {"universe": int(r[1]), "amount": float(r[2])} for r in cur.fetchall()}


def draw_sample(rows: list[dict], per_stratum: int, seed: int) -> tuple[list[dict], dict]:
    """分层抽样：全样本层全取，三个小额层各随机抽 per_stratum 笔。

    可复现性靠两点：
      1. 候选按 trans_id 升序排列后再抽（不依赖数据库返回顺序）
      2. 每层单独用 seed + 层序号 起随机数发生器（不依赖上一层的抽样次数）
    """
    picked, meta = [], {}
    all_layer = [r for r in rows if r["stratum"] == "all"]
    picked.extend(all_layer)
    meta["all"] = {"universe": len(all_layer), "sampled": len(all_layer)}

    for i, st in enumerate(SAMPLE_STRATA):
        pool = sorted((r for r in rows if r["stratum"] == st), key=lambda r: r["trans_id"])
        n = min(per_stratum, len(pool))
        rng = random.Random(seed + i)          # 每层独立种子，层间互不影响
        sampled = rng.sample(pool, n)
        picked.extend(sampled)
        meta[st] = {"universe": len(pool), "sampled": n}
    return sorted(picked, key=lambda r: r["tm"]), meta


# -------------------------------------------------------------
# 二、渲染
# -------------------------------------------------------------
STRATUM_LABEL = {
    "all": "全标 ≥20元",
    "s1_0_5": "抽样 0~5元",
    "s2_5_10": "抽样 5~10元",
    "s3_10_20": "抽样 10~20元",
}

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>闲置盘点工作台 · 大学生闲置消费识别与预警系统</title>
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
  .wrap{max-width:1220px;margin:0 auto;padding:20px 18px 100px}
  h1{font-size:20px;margin:0 0 4px}
  h2{font-size:14px;margin:0 0 8px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
  .steps{display:flex;gap:10px;flex-wrap:wrap}
  .step{flex:1 1 240px;background:var(--blue-l);border:1px solid #cfe0f2;border-radius:8px;padding:10px 12px;font-size:13px}
  .step b{color:var(--blue);display:block;margin-bottom:3px}
  .kpis{display:flex;gap:10px;flex-wrap:wrap}
  .kpi{flex:1 1 140px;background:#f7f9fc;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
  .kpi .v{font-size:20px;font-weight:700}
  .kpi .k{font-size:12px;color:var(--muted)}
  .kpi.hi .v{color:var(--gold)}
  .bar{height:8px;background:#e3e8ef;border-radius:99px;overflow:hidden;margin-top:8px}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),#7ea3c9);width:0}
  table.lay{width:100%;border-collapse:collapse;font-size:13px}
  table.lay th,table.lay td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right}
  table.lay th:first-child,table.lay td:first-child{text-align:left}
  table.lay thead th{color:var(--muted);font-weight:400;font-size:12px}
  .tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  input,select,button{font:inherit;color:inherit}
  input[type=text],input[type=number],select{border:1px solid var(--line);background:#fff;border-radius:6px;padding:5px 8px}
  input[type=text]{min-width:180px}
  button{border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 12px;cursor:pointer}
  button:hover{border-color:var(--blue);color:var(--blue)}
  button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
  button.primary:hover{background:#4c6d94;color:#fff}
  button.warn{border-color:#e3c9c5;color:var(--red)}
  .chk{display:inline-flex;gap:5px;align-items:center;font-size:13px;color:var(--muted);cursor:pointer}
  table.main{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  table.main thead th{position:sticky;top:0;background:#f2f5f9;text-align:left;font-size:12px;color:var(--muted);
           padding:8px;border-bottom:1px solid var(--line);white-space:nowrap;z-index:2}
  table.main tbody td{padding:6px 8px;border-bottom:1px solid #edf0f4;vertical-align:middle}
  tbody tr.row{cursor:pointer}
  tbody tr.row:hover{background:#f6f9fd}
  tbody tr.row.done{background:var(--green-l)}
  tbody tr.row.done:hover{background:#e7f4ec}
  tbody tr.row.out{background:var(--gray-l);opacity:.62}
  tr.detail{display:none;background:#fafbfd}
  tr.detail.open{display:table-row}
  tr.detail td{padding:10px 8px 14px;border-bottom:2px solid #e6ebf2}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);color:var(--muted);background:#fff}
  .tag.warn{background:var(--gold-l);border-color:#e8d49b;color:var(--gold)}
  .tag.ok{background:var(--green-l);border-color:#bfdccb;color:var(--green)}
  .tag.gray{background:var(--gray-l);color:var(--gray)}
  .tag.all{background:var(--blue-l);border-color:#cfe0f2;color:var(--blue)}
  .muted{color:var(--muted)}
  .mini{font-size:12px}
  .dt{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 16px;align-items:start}
  .dt label{font-size:12px;color:var(--muted);display:block;margin-bottom:3px}
  .dt select,.dt input{width:100%}
  .foot{position:fixed;left:0;right:0;bottom:0;background:rgba(251,252,253,.96);
        border-top:1px solid var(--line);backdrop-filter:blur(6px)}
  .foot .inner{max-width:1220px;margin:0 auto;padding:10px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .grow{flex:1}
  dialog{border:1px solid var(--line);border-radius:10px;padding:0;max-width:920px;width:92%}
  dialog::backdrop{background:rgba(20,28,38,.45)}
  dialog .dh{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
  dialog .dh b{flex:1}
  dialog textarea{width:100%;height:46vh;border:0;border-bottom:1px solid var(--line);
                  font:12px/1.5 Consolas,Monaco,monospace;padding:12px 16px;resize:vertical}
  dialog .df{padding:10px 16px;display:flex;gap:8px;justify-content:flex-end}
  @media print{
    body{background:#fff}.foot,.tools,.steps{display:none!important}
    tr.detail{display:table-row!important}.wrap{padding:0}
    table.main thead th{position:static}
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>闲置盘点工作台 <span class="tag all">分层抽样版</span></h1>
  <div class="sub">待标 <b id="h-total">0</b> 笔 ｜ 全标层 ≥¥20 共 <b>__N_ALL__</b> 笔 ｜
    抽样层各 __PER__ 笔（随机种子 <b>__SEED__</b>，可复现）</div>

  <div class="card steps">
    <div class="step"><b>① 补品类</b>「现分类」是电商综合的先改（共 <b id="h-need">0</b> 笔）。<br>
      <span class="muted">如果这笔其实是即时消耗——超市零食、洗衣、话费——就改成「生活服务 / 餐饮 / 交通」，它会自动移出标注范围，不必再判闲置。</span></div>
    <div class="step"><b>② 打闲置</b>点任意一行展开：在用 / 闲置 → 程度 → 原因 → 实际用了几天。<br>
      <span class="muted">同一个东西买过好几次的，按最近那笔为准即可。</span></div>
    <div class="step"><b>③ 导出</b>右下角「导出 SQL」→ 复制发我。进度自动存在浏览器里，关掉不丢。</div>
  </div>

  <div class="card">
    <div class="kpis">
      <div class="kpi"><div class="v" id="k-total">0</div><div class="k">本次待标</div></div>
      <div class="kpi hi"><div class="v" id="k-need">0</div><div class="k">需补品类</div></div>
      <div class="kpi"><div class="v" id="k-amt">¥0</div><div class="k">涉及金额</div></div>
      <div class="kpi"><div class="v" id="k-done">0</div><div class="k">已判定</div></div>
      <div class="kpi"><div class="v" id="k-out">0</div><div class="k">已移出</div></div>
    </div>
    <div class="bar"><i id="bar"></i></div>
    <div class="mini muted" id="hint" style="margin-top:6px">点行展开明细 →</div>
  </div>

  <div class="card">
    <h2>抽样设计（写进说明书用）</h2>
    <table class="lay">
      <thead><tr><th>层</th><th>总体笔数</th><th>本次标注</th><th>抽样比</th><th>加权倍率</th><th>总体金额</th><th>金额占比</th></tr></thead>
      <tbody>__LAYTABLE__</tbody>
    </table>
    <div class="mini muted" style="margin-top:8px">
      加权倍率 = 总体笔数 / 本次标注笔数，即在统计时“一笔标注代表几笔真实交易”。
      未标注的小额交易不是被丢掉，而是靠这个倍率还原回总体 —— 见
      <code>v_category_idle_rate_weighted</code>。
    </div>
  </div>

  <div class="card tools">
    <input type="text" id="q" placeholder="搜对方 / 商品说明…">
    <select id="f-stratum"><option value="">全部层</option>__STROPT__</select>
    <select id="f-cat"><option value="">全部分类</option></select>
    <select id="f-state">
      <option value="">全部状态</option>
      <option value="todo">只看未判定</option>
      <option value="done">只看已判定</option>
      <option value="idle">只看闲置</option>
      <option value="keep">只看仍在用</option>
      <option value="out">只看已移出</option>
    </select>
    <select id="f-sort">
      <option value="tm">按时间排</option>
      <option value="amt">按金额从大到小</option>
    </select>
    <label class="chk"><input type="checkbox" id="f-need"> 只看需补品类</label>
    <span class="grow"></span>
    <button id="btn-expand">全部展开</button>
    <button id="btn-print">打印清单</button>
    <button id="btn-reset" class="warn">清空填写</button>
  </div>

  <table class="main">
    <thead><tr>
      <th style="width:130px">时间</th>
      <th style="width:74px">抽样层</th>
      <th>对方</th>
      <th>商品说明</th>
      <th style="width:80px" class="num">金额</th>
      <th style="width:92px">现分类</th>
      <th style="width:150px">补品类</th>
      <th style="width:110px">闲置?</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
  <div class="sub" style="margin-top:10px">共 <b id="k-shown">0</b> 行显示中。</div>
</div>

<div class="foot"><div class="inner">
  <b>进度</b><span id="p-done" class="muted">0 / 0</span>
  <span class="grow"></span>
  <button id="btn-csv">导出 CSV</button>
  <button id="btn-sql" class="primary">导出 SQL</button>
</div></div>

<dialog id="dlg">
  <div class="dh"><b id="dlg-t">SQL</b><button id="dlg-close">关闭</button></div>
  <textarea id="dlg-body" spellcheck="false"></textarea>
  <div class="df"><span class="grow muted mini" id="dlg-note"></span><button id="dlg-copy" class="primary">复制全部</button></div>
</dialog>

<script>
const ROWS = __DATA__;
const CATS = __CATS__;
const META = __META__;
const SEED = __SEED__;
const SLABEL = __SLABEL__;
const CATNAME = {}; CATS.forEach(c=>CATNAME[c.code]=c.name);
const DURABLE = {}; CATS.forEach(c=>DURABLE[c.code]=c.durable);
const REASONS = ["","冲动消费","重复购买","与描述不符","尺寸不合适","三分钟热度","促销囤货","其他"];
const KEY = "idle_workbench_v2";

let state = {};
let open = {};

function load(){ try{ state = JSON.parse(localStorage.getItem(KEY)||"{}"); }catch(e){ state={}; } }
function save(){ localStorage.setItem(KEY, JSON.stringify(state)); }
function S(id){ if(!state[id]) state[id]={cat:"",idle:"",deg:"",reason:"",days:"",note:""}; return state[id]; }
function effCat(r){ const s=state[r.id]; return (s && s.cat) ? s.cat : r.category_code; }
function movedOut(r){ return DURABLE[effCat(r)] === 0; }          // 改到即时消耗分类 = 移出标注范围
function isDone(r){
  if(movedOut(r)) return true;
  const s = state[r.id];
  return !!(s && (s.idle !== "" && s.idle !== undefined));
}
const $ = s=>document.querySelector(s);
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function fillFilters(){
  const seen={};
  ROWS.forEach(r=>{ if(!seen[r.category_code]){ seen[r.category_code]=1;
    $("#f-cat").insertAdjacentHTML("beforeend",`<option value="${r.category_code}">${r.category_name}</option>`); }});
}
function filtered(){
  const q=$("#q").value.trim().toLowerCase(), fs=$("#f-stratum").value, fc=$("#f-cat").value,
        st=$("#f-state").value, need=$("#f-need").checked;
  const out = ROWS.filter(r=>{
    if(fs && r.stratum!==fs) return false;
    if(fc && r.category_code!==fc) return false;
    if(need && r.category_code!=="ECOMM") return false;
    if(q && (r.cp+r.pd).toLowerCase().indexOf(q)<0) return false;
    const s=state[r.id]||{}, moved=movedOut(r);
    // 已移出标注范围的（品类被改判为即时消耗）：只在"全部状态"和"只看已移出"里出现
    if(st==="out") return moved;
    if(moved) return st==="";
    if(st==="todo" && isDone(r)) return false;
    if(st==="done" && !isDone(r)) return false;
    if(st==="idle" && s.idle!=="1") return false;
    if(st==="keep" && s.idle!=="0") return false;
    return true;
  });
  if($("#f-sort").value==="amt") out.sort((a,b)=>b.amount-a.amount);
  return out;
}
function catOptions(r){
  const cur = effCat(r);
  const g = (t, list) => `<optgroup label="${t}">` + list.map(c=>
      `<option value="${c.code}"${c.code===cur?" selected":""}>${c.name}</option>`).join("") + `</optgroup>`;
  return g("耐用品（需判闲置）", CATS.filter(c=>c.durable===1))
       + g("即时消耗 / 服务（改到这里＝移出）", CATS.filter(c=>c.durable===0));
}
function rowHTML(r){
  const s=S(r.id), needCat = r.category_code==="ECOMM", moved = movedOut(r);
  const idle = s.idle===""||s.idle===undefined ? "—" : (s.idle==="1" ? "闲置" : "在用");
  const cls = "row" + (isDone(r)&&!moved?" done":"") + (moved?" out":"");
  const catTag = needCat ? `<span class="tag warn">${r.category_name}</span>`
                         : `<span class="tag">${r.category_name}</span>`;
  let h = `<tr class="${cls}" data-id="${r.id}">
    <td class="mini muted">${r.tm}</td>
    <td class="mini"><span class="tag ${r.stratum==="all"?"all":"gray"}">${SLABEL[r.stratum]}</span></td>
    <td class="mini">${esc(r.cp)}</td>
    <td class="mini muted">${esc(r.pd)}</td>
    <td class="num"><b>${r.amount.toFixed(2)}</b></td>
    <td>${catTag}</td>
    <td><select class="c${effCat(r)==="ECOMM"?" need":""}" data-id="${r.id}">${catOptions(r)}</select></td>
    <td class="mini" data-cell="idle">${moved?'<span class="tag gray">已移出</span>':idle}</td>
  </tr>`;
  h += `<tr class="detail${open[r.id]?" open":""}" data-d="${r.id}"><td colspan="8">
    <div class="dt">
      <div><label>是否闲置</label><select class="i" data-id="${r.id}">
        <option value=""${s.idle===""||s.idle===undefined?" selected":""}>未判定</option>
        <option value="0"${s.idle==="0"?" selected":""}>在用（没白花）</option>
        <option value="1"${s.idle==="1"?" selected":""}>闲置（白花了）</option></select></div>
      <div><label>闲置程度（在用可留空）</label><select class="g" data-id="${r.id}">
        <option value=""${s.deg===""||s.deg===undefined?" selected":""}>未选</option>
        <option value="1"${s.deg==="1"?" selected":""}>1 轻度（偶尔用）</option>
        <option value="2"${s.deg==="2"?" selected":""}>2 重度（几乎不用）</option>
        <option value="3"${s.deg==="3"?" selected":""}>3 完全浪费（一次没用）</option></select></div>
      <div><label>闲置原因</label><select class="r" data-id="${r.id}">
        ${REASONS.map(x=>`<option value="${x}"${s.reason===x?" selected":""}>${x||"未选"}</option>`).join("")}</select></div>
      <div><label>实际用了几天（自估）</label><input type="number" min="0" class="d" data-id="${r.id}" value="${s.days||""}" placeholder="如 3"></div>
      <div style="grid-column:1/-1"><label>备注（可留空）</label>
        <input type="text" class="n" data-id="${r.id}" value="${esc(s.note||"")}" placeholder="如：拼多多买的收纳盒，现在还在用"></div>
    </div>
    <div class="mini muted" style="margin-top:8px">交易号 #${r.id} ｜ 原分类 ${r.category_code} ｜ 抽样层 ${r.stratum}
      ｜ 统计时本层一笔代表 ${(META[r.stratum].universe/META[r.stratum].sampled).toFixed(2)} 笔</div>
  </td></tr>`;
  return h;
}
function refreshRow(id){
  const r = ROWS.find(x=>x.id==id); if(!r) return;
  const tr = document.querySelector('tr.row[data-id="'+id+'"]'); if(!tr) return;
  const s = state[id]||{}, moved = movedOut(r);
  tr.classList.toggle("done", isDone(r) && !moved);
  tr.classList.toggle("out", moved);
  const cell = tr.querySelector('[data-cell="idle"]');
  if(cell) cell.innerHTML = moved ? '<span class="tag gray">已移出</span>'
      : (s.idle===""||s.idle===undefined ? "—" : (s.idle==="1" ? "闲置" : "在用"));
  updateStats();
}
function render(){
  const list = filtered();
  $("#tb").innerHTML = list.map(rowHTML).join("");
  $("#k-shown").textContent = list.length;
  updateStats();
}
function updateStats(){
  const total = ROWS.length;
  const need = ROWS.filter(r=>r.category_code==="ECOMM").length;
  const amt = ROWS.reduce((a,r)=>a+r.amount,0);
  const outCnt = ROWS.filter(movedOut).length;
  const done = ROWS.filter(isDone).length;
  $("#k-total").textContent=total; $("#k-need").textContent=need;
  $("#k-amt").textContent="¥"+amt.toFixed(0); $("#k-done").textContent=done;
  $("#k-out").textContent=outCnt; $("#h-need").textContent=need; $("#h-total").textContent=total;
  $("#bar").style.width=(done/total*100).toFixed(1)+"%";
  $("#p-done").textContent = done + " / " + total;
  $("#hint").textContent = done===0 ? "点任意一行展开，开始判定 →"
      : (done<total ? `还剩 ${total-done} 笔。进度自动保存。` : "全部处理完，点右下角「导出 SQL」交回。");
}

document.addEventListener("click", e=>{
  const tr = e.target.closest("tr.row");
  if(tr && !e.target.closest("select")){ open[tr.dataset.id]=!open[tr.dataset.id]; render(); return; }
  const d = e.target.closest("tr.detail");
  if(d && e.target.closest("td") && !e.target.closest("select,input,label")){ open[d.dataset.d]=false; render(); }
});
document.addEventListener("change", e=>{
  const el=e.target, id=el.dataset.id; if(!id) return; const s=S(id);
  if(el.classList.contains("c")){ s.cat=el.value; el.classList.toggle("need", el.value==="ECOMM"); }
  else if(el.classList.contains("i")){ s.idle=el.value; if(el.value==="0") s.deg=""; }
  else if(el.classList.contains("g")) s.deg=el.value;
  else if(el.classList.contains("r")) s.reason=el.value;
  else if(el.classList.contains("d")) s.days=el.value;
  save();
  const mayLeave = (el.classList.contains("c") && $("#f-need").checked)
                || (el.classList.contains("c") && $("#f-state").value);
  if(mayLeave) render(); else refreshRow(id);
});
document.addEventListener("input", e=>{
  const el=e.target;
  if(el.id==="q"){ render(); return; }
  if(el.classList.contains("n")){ S(el.dataset.id).note=el.value; save(); }
});
["f-stratum","f-cat","f-state","f-sort","f-need"].forEach(id=>$("#"+id).addEventListener("change",render));

$("#btn-expand").onclick=()=>{
  const anyClosed = ROWS.some(r=>!open[r.id]);
  ROWS.forEach(r=>open[r.id]=anyClosed); render();
  $("#btn-expand").textContent = anyClosed ? "全部收起" : "全部展开";
};
$("#btn-print").onclick=()=>window.print();
$("#btn-reset").onclick=()=>{ if(!confirm("清空所有已填内容？此操作不可撤销。")) return;
  state={}; save(); render(); };

function download(name, text){
  const b=new Blob(["\ufeff"+text],{type:"text/csv;charset=utf-8"});
  const a=document.createElement("a"); a.href=URL.createObjectURL(b); a.download=name; a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),4000);
}
$("#btn-csv").onclick=()=>{
  const head=["trans_id","交易时间","抽样层","对方","商品说明","金额","原分类","最终品类","品类名称","是否闲置","闲置程度","闲置原因","使用天数","备注"];
  const lines=[head.join(",")];
  ROWS.forEach(r=>{
    const s=state[r.id]||{};
    const cat=effCat(r);
    const cells=[r.id,r.tm,r.stratum,r.cp,r.pd,r.amount.toFixed(2),r.category_code,cat,CATNAME[cat],
      movedOut(r)?"已移出":(s.idle===""||s.idle===undefined?"":(s.idle==="1"?"闲置":"在用")),
      s.deg||"",s.reason||"",s.days||"",s.note||""];
    lines.push(cells.map(v=>{ v=String(v); return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; }).join(","));
  });
  download("闲置盘点_填写结果.csv", lines.join("\r\n"));
};
$("#btn-sql").onclick=()=>{
  const upd=[], ins=[], skipped=[];
  ROWS.forEach(r=>{
    const s=state[r.id]||{}, cat=effCat(r);
    if(cat!==r.category_code)
      upd.push(`UPDATE t_transaction SET category_id=(SELECT category_id FROM t_category WHERE category_code='${cat}'), category_src='manual' WHERE trans_id=${r.id};   -- ${r.category_code} → ${cat}`);
    if(movedOut(r)){ skipped.push(`-- #${r.id} ${r.tm} ¥${r.amount.toFixed(2)} 已改判为即时消耗（${cat}），不标注`); return; }
    if(s.idle==="1"||s.idle==="0"){
      const deg = s.idle==="1" ? (s.deg||"1") : "0";
      const rs  = s.reason ? `'${s.reason}'` : "NULL";
      const dy  = (s.days!==""&&s.days!==undefined) ? s.days : "NULL";
      const nt  = s.note ? `'${s.note.replace(/'/g,"''")}'` : "NULL";
      ins.push(`INSERT INTO t_idle_label (trans_id,is_idle,idle_degree,idle_reason,used_days,sample_stratum,note)`
             + ` VALUES (${r.id},${s.idle},${deg},${rs},${dy},'${r.stratum}',${nt})`
             + ` ON DUPLICATE KEY UPDATE is_idle=VALUES(is_idle),idle_degree=VALUES(idle_degree),`
             + `idle_reason=VALUES(idle_reason),used_days=VALUES(used_days),sample_stratum=VALUES(sample_stratum),note=VALUES(note);`);
    }
  });
  const strata = Object.keys(META).map(k=>`--   ${k}: 总体 ${META[k].universe} 笔 / 标注 ${META[k].sampled} 笔 / 加权 ${(META[k].universe/META[k].sampled).toFixed(4)}`).join("\n");
  const txt =
`-- ============================================================
-- 闲置盘点回填   ${new Date().toLocaleString("zh-CN")}
-- 分层抽样：种子 ${SEED}，每层抽样笔数见下
${strata}
--
-- 一、手工修正的品类（${upd.length} 条，含把即时消耗类移出标注范围）
-- ============================================================
USE spending_waste;
${upd.join("\n") || "-- （无）"}

-- ============================================================
-- 二、闲置标注（${ins.length} 条）
-- ============================================================
${ins.join("\n") || "-- （无）"}

-- ============================================================
-- 三、已移出标注范围（${skipped.length} 条，仅备查，不写标签表）
-- ============================================================
${skipped.join("\n") || "-- （无）"}
`;
  $("#dlg-t").textContent = `导出 SQL（品类 ${upd.length} · 标注 ${ins.length} · 移出 ${skipped.length}）`;
  $("#dlg-body").value = txt;
  $("#dlg-note").textContent = "复制后发我，我会先跑一致性校验再写库。";
  $("#dlg").showModal();
};
$("#dlg-close").onclick=()=>$("#dlg").close();
$("#dlg-copy").onclick=()=>{ const t=$("#dlg-body"); t.select(); document.execCommand("copy"); $("#dlg-note").textContent="已复制 ✓"; };

load(); fillFilters(); render();
</script>
</body>
</html>
"""


def build_html(rows, cats, meta, seed, uni_stats) -> str:
    order = ("all",) + SAMPLE_STRATA
    total_uni = sum(uni_stats[k]["universe"] for k in order)
    total_uni_amt = sum(uni_stats[k]["amount"] for k in order)
    strata_rows = []
    for k in order:
        u, s = meta[k]["universe"], meta[k]["sampled"]
        amt = uni_stats[k]["amount"]
        strata_rows.append(
            f"<tr><td>{STRATUM_LABEL[k]}</td><td>{u}</td><td>{s}</td>"
            f"<td>{s/u*100:.1f}%</td><td>{u/s:.2f}</td>"
            f"<td>¥{amt:,.2f}</td><td>{amt/total_uni_amt*100:.1f}%</td></tr>")
    strata_rows.append(
        f"<tr style='font-weight:700'><td>合计</td>"
        f"<td>{total_uni}</td>"
        f"<td>{sum(meta[k]['sampled'] for k in order)}</td><td>—</td><td>—</td>"
        f"<td>¥{total_uni_amt:,.2f}</td><td>100%</td></tr>")

    stropt = "".join(f'<option value="{k}">{STRATUM_LABEL[k]}</option>' for k in order)

    return (TEMPLATE
            .replace("__DATA__", json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
            .replace("__CATS__", json.dumps(cats, ensure_ascii=False, separators=(",", ":")))
            .replace("__META__", json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
            .replace("__SLABEL__", json.dumps(STRATUM_LABEL, ensure_ascii=False))
            .replace("__LAYTABLE__", "".join(strata_rows))
            .replace("__STROPT__", stropt)
            .replace("__SEED__", str(seed))
            .replace("__PER__", str(DEFAULT_PER_STRATUM))
            .replace("__N_ALL__", str(meta["all"]["universe"])))


def main() -> int:
    ap = argparse.ArgumentParser(description="生成分层抽样的闲置盘点工作台")
    ap.add_argument("--out", default="闲置盘点工作台.html")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--per-stratum", type=int, default=DEFAULT_PER_STRATUM)
    args = ap.parse_args()

    conn = pipeline.connect()
    rows = fetch_candidates(conn)
    cats = fetch_categories(conn)
    uni_stats = fetch_stratum_stats(conn)
    conn.close()

    picked, meta = draw_sample(rows, args.per_stratum, args.seed)
    html = build_html(picked, cats, meta, args.seed, uni_stats)
    with io.open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"候选全集 {len(rows)} 笔")
    for k in ("all",) + SAMPLE_STRATA:
        m = meta[k]
        print(f"  {k:10} 总体 {m['universe']:4} 笔 / ¥{uni_stats[k]['amount']:>9,.2f}"
              f" → 本次标注 {m['sampled']:3} 笔（1 笔代表 {m['universe']/m['sampled']:.2f} 笔）")
    print(f"本次待标合计 {len(picked)} 笔")
    print(f"输出：{os.path.abspath(args.out)}  ({len(html.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
