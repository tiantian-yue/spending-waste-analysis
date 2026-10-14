#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「浪费结构报告」HTML —— 浪费体检结论的可视化交付物。

数据来源全部走视图，不自己拼 WHERE：
  v_checkup_total   —— 一句话题眼（浪费多少 / 占比）
  v_checkup_summary —— 两层各自贡献
  品类维度按视图口径聚合
  big_item 层逐笔
"""
import os
import sys

import pymysql

DB = dict(host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3306")),
          user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", "123456"),
          database=os.getenv("DB_NAME", "spending_waste"), charset="utf8mb4",
          cursorclass=pymysql.cursors.DictCursor)

OUT = sys.argv[1] if len(sys.argv) > 1 else "浪费结构报告.html"


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def money(x):
    return f"{x:,.2f}"


def main():
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            tot = q(cur, "SELECT * FROM v_checkup_total")[0]
            layers = q(cur, "SELECT * FROM v_checkup_summary ORDER BY waste_amt DESC")
            cats = q(cur, """
                SELECT c.category_code code, c.category_name name, COUNT(*) n,
                       ROUND(SUM(v.amount),2) pool_amt,
                       ROUND(SUM(CASE WHEN l.sample_stratum='big_item' AND l.is_idle=1 THEN v.amount
                                      WHEN l.sample_stratum='cat_prior'
                                      THEN v.amount*CAST(REGEXP_SUBSTR(l.note,'[0-9]+') AS DECIMAL(5,2))/100
                                      ELSE 0 END),2) waste_amt,
                       SUM(CASE WHEN l.sample_stratum='big_item' THEN 1 ELSE 0 END) big_n,
                       SUM(CASE WHEN l.sample_stratum='big_item' AND l.is_idle=1 THEN 1 ELSE 0 END) big_idle_n,
                       MAX(CAST(REGEXP_SUBSTR(l.note,'[0-9]+') AS DECIMAL(5,2))) pct
                FROM t_idle_label l
                JOIN v_idle_candidate v ON v.trans_id=l.trans_id
                JOIN t_category c ON c.category_id=v.category_id
                GROUP BY c.category_code, c.category_name
                ORDER BY waste_amt DESC
            """)
            bigs = q(cur, """
                SELECT l.trans_id id, DATE(v.trade_time) d,
                       ROUND(v.amount,2) amt, c.category_name cat,
                       COALESCE(g.goods_title, v.counterparty, v.product_desc) goods,
                       l.is_idle idle
                FROM t_idle_label l
                JOIN v_idle_candidate v ON v.trans_id=l.trans_id
                JOIN t_category c ON c.category_id=v.category_id
                LEFT JOIN t_goods_detail g ON g.trans_id=l.trans_id
                WHERE l.sample_stratum='big_item' ORDER BY v.amount DESC
            """)
    finally:
        conn.close()

    waste, pool = float(tot["waste_amt"]), float(tot["pool_amt"])
    rate = float(tot["waste_rate_pct"])
    idle_bigs = [b for b in bigs if b["idle"] == 1]
    max_cat_waste = max(float(c["waste_amt"]) for c in cats) or 1

    def cat_row(c):
        w, p = float(c["waste_amt"]), float(c["pool_amt"])
        w_rate = w / p * 100 if p else 0
        bw = w / max_cat_waste * 100
        return f"""<tr>
      <td><b>{c['name']}</b><br><span class="muted">{c['code']} · {c['n']} 笔</span></td>
      <td class="num">{money(p)}</td>
      <td class="num" style="color:var(--red)">{money(w)}</td>
      <td class="num">{w_rate:.1f}%</td>
      <td><div class="bar"><i style="width:{bw:.0f}%"></i></div></td>
      <td>{('先验 ' + str(int(c['pct'])) + '%') if c['pct'] is not None else '—'}</td>
      <td>{c['big_idle_n']}/{c['big_n']}</td>
    </tr>"""

    def big_row(b):
        idle = b["idle"] == 1
        tag = ('<span class="tag hard">闲置</span>' if idle
               else '<span class="tag ok">在用</span>')
        goods = (b["goods"] or "（无商品名）")[:46]
        return (f'<tr{" class=\'idle-row\'" if idle else ""}>'
                f'<td class="muted">#{b["id"]}</td><td class="muted">{b["d"]}</td>'
                f'<td>{goods}</td><td class="muted">{b["cat"]}</td>'
                f'<td class="num">{money(float(b["amt"]))}</td><td>{tag}</td></tr>')

    idle_big_rows = "".join(big_row(b) for b in idle_bigs)
    other_big_rows = "".join(big_row(b) for b in bigs if b["idle"] != 1)
    idle_big_amt = sum(float(b["amt"]) for b in idle_bigs)

    def layer_row(l):
        w, p = float(l["waste_amt"]), float(l["pool_amt"])
        note = ("逐笔真值，可直接进训练集" if l["layer"] == "big_item"
                else "先验推算，需加权、不能当逐笔事实")
        return (f'<tr><td><b>{l["layer_desc"]}</b><br>'
                f'<span class="muted">{l["layer"]}</span></td>'
                f'<td class="num">{l["n"]}</td>'
                f'<td class="num">{money(p)}</td>'
                f'<td class="num" style="color:var(--red)">{money(w)}</td>'
                f'<td class="num">{w / waste * 100:.0f}%</td>'
                f'<td class="muted">{note}</td></tr>')

    layer_rows = "".join(layer_row(l) for l in layers)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>浪费结构报告 · 大学生闲置消费识别与预警系统</title>
<style>
  :root{{--bg:#f7f8fa;--card:#fff;--ink:#1c2024;--ink2:#4a5158;--ink3:#7a838c;
    --line:#e3e6ea;--red:#c0392b;--redbg:#fdf0ee;--green:#1f7a4d;--greenbg:#eef8f1;
    --blue:#1f5fa8;--bluebg:#eef4fc;--amber:#a8700f;--amberbg:#fdf7e8}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
  .wrap{{max-width:1020px;margin:0 auto;padding:40px 22px 80px}}
  header{{border-bottom:3px solid var(--ink);padding-bottom:20px;margin-bottom:34px}}
  h1{{font-size:26px;margin:0 0 8px}} .sub{{color:var(--ink3);font-size:13.5px}}
  h2{{font-size:19px;margin:46px 0 14px;padding-left:12px;border-left:4px solid var(--ink)}}
  h3{{font-size:16px;margin:26px 0 10px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0}}
  .box{{border-radius:10px;padding:15px 18px;margin:14px 0;border:1px solid}}
  .box.red{{background:var(--redbg);border-color:#f0cfca}}
  .box.blue{{background:var(--bluebg);border-color:#cddff5}}
  .box.amber{{background:var(--amberbg);border-color:#f0e0b8}}
  .box .t{{font-weight:700;margin-bottom:6px;display:block}}
  .box.red .t{{color:var(--red)}} .box.blue .t{{color:var(--blue)}} .box.amber .t{{color:var(--amber)}}
  table{{width:100%;border-collapse:collapse;margin:14px 0;font-size:14px}}
  th,td{{border:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}}
  th{{background:#f2f4f7;font-weight:600;color:var(--ink2)}}
  tr:nth-child(even) td{{background:#fbfcfd}}
  tr.idle-row td{{background:#fdf6f5}}
  tr.idle-row:nth-child(even) td{{background:#fcf1ef}}
  code{{background:#eef1f5;padding:1.5px 5px;border-radius:4px;font-size:13px;
    font-family:"SFMono-Regular",Consolas,monospace}}
  .tag{{display:inline-block;font-size:12px;padding:1px 7px;border-radius:20px;border:1px solid}}
  .tag.hard{{color:var(--red);border-color:#e8c4be;background:#fdf1ef}}
  .tag.ok{{color:var(--green);border-color:#c4e0d1;background:#f0f9f4}}
  .num{{font-variant-numeric:tabular-nums;font-weight:600}}
  .kpi{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
  .kpi div{{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
  .kpi .lbl{{font-size:12.5px;color:var(--ink3);display:block;margin-bottom:4px}}
  .kpi .val{{font-size:21px;font-weight:700;font-variant-numeric:tabular-nums}}
  .bar{{height:9px;background:#e9edf1;border-radius:5px;overflow:hidden;min-width:70px}}
  .bar i{{display:block;height:100%;background:#c0392b;opacity:.75}}
  .muted{{color:var(--ink3);font-size:13px}}
  .scroll{{overflow-x:auto}}
  footer{{margin-top:60px;padding-top:18px;border-top:1px solid var(--line);color:var(--ink3);font-size:13px}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>浪费结构报告</h1>
  <div class="sub">大学生闲置消费识别与预警系统 · 两年的钱，有多少是白花的</div>
</header>

<div class="kpi">
  <div><span class="lbl">候选池（可闲置的实物/耐用品）</span><span class="val">¥{money(pool)}</span></div>
  <div><span class="lbl">其中判定为浪费</span><span class="val" style="color:var(--red)">¥{money(waste)}</span></div>
  <div><span class="lbl">浪费率</span><span class="val" style="color:var(--red)">{rate:.1f}%</span></div>
  <div><span class="lbl">真在用（不算浪费）</span><span class="val" style="color:var(--green)">¥{money(pool - waste)}</span></div>
</div>

<div class="box red">
  <span class="t">一句话结论</span>
  两年下来，买回来的实物/耐用品里，<b>大约每 3 块钱就有 1 块是白花的</b>
  ——合计 <b>¥{money(waste)}</b>，占候选池的 {rate:.1f}%（约三分之一）。
  放到全部支出（¥83,094.45）里看，相当于 <b>{waste / 83094.45 * 100:.1f}%</b>
  <span class="muted">（差额是吃饭、交通、话费这些花完就没的服务类消费，它们本来就不参与闲置判定）</span>。
  最大的一笔不是零星冲动，而是一件「买了却一年只用得上两个月」的大件（¥{money(float(idle_bigs[0]["amt"]))}）。
</div>

<h2>一、这份数字是怎么来的</h2>

<p>「这件东西到底有没有闲置」这句话，账单里一个字都没有——收单机构只记到付了多少钱。
这条信息只存在于人的记忆里。所以任何方案都绕不开"人的输入"，
问题只是<b>把输入的单位从「笔」换成「类」</b>：</p>

<table>
<tr><th>层</th><th>怎么得到</th><th>人工量</th><th>覆盖</th><th>精度</th></tr>
<tr><td><b>L1 规则硬排除</b></td><td>食品生鲜、话费/驾校等服务：消费完瞬间就没的东西，不存在"闲置"</td>
    <td class="num">0 次</td><td>自动掉出候选池</td><td>确定性</td></tr>
<tr><td><b>L2 品类级</b></td><td>6 个品类各选一档：基本都在用／少部分闲置／约一半／大多闲置</td>
    <td class="num">6 次点击</td><td>{tot['cat_n']} 笔 · ¥{money(float([l for l in layers if l['layer']=='cat_prior'][0]['pool_amt']))}</td>
    <td>先验推算</td></tr>
<tr><td><b>L3 单品级</b></td><td>≥¥100 的笔逐笔问：闲置 / 在用</td>
    <td class="num">18 次点击</td><td>{tot['big_n']} 笔 · ¥{money(float([l for l in layers if l['layer']=='big_item'][0]['pool_amt']))}</td>
    <td>逐笔真值</td></tr>
</table>

<p class="muted">逐笔标注要 705 笔 × 5 字段 ≈ 3,525 次输入；这套做法 <b>24 次点击</b>出结论。
代价是品类内的个体差异被平均掉了——换来的是<b>可迁移性</b>：换任何人的账单，
同样只需回答这几个品类问题 + 确认他自己的大额笔。</p>

<h3>两层各自贡献了多少</h3>

<table>
<tr><th>层</th><th>笔数</th><th>池金额</th><th>浪费金额</th><th>占浪费总量</th><th>可比性</th></tr>
{layer_rows}
<tr style="background:#f2f4f7;font-weight:700">
  <td>合计</td><td class="num">{int(tot['big_n']) + int(tot['cat_n'])}</td>
  <td class="num">{money(pool)}</td><td class="num" style="color:var(--red)">{money(waste)}</td>
  <td>100%</td><td>—</td></tr>
</table>

<div class="box amber">
  <span class="t">为什么 L3 只占 {int(tot['big_n'])} 笔，却贡献了 {idle_big_amt / waste * 100:.0f}% 的浪费</span>
  因为大额笔本身就是浪费的重灾区：¥{money(float([l for l in layers if l['layer']=='big_item'][0]['pool_amt']))} 的池子里，
  逐笔确认下来有 <b>{money(idle_big_amt)}</b> 是闲置（占该层 {idle_big_amt / float([l for l in layers if l['layer']=='big_item'][0]['pool_amt']) * 100:.0f}%）；
  而小额层 ¥{money(float([l for l in layers if l['layer']=='cat_prior'][0]['pool_amt']))} 按先验推算只有
  <b>{money(float([l for l in layers if l['layer']=='cat_prior'][0]['waste_amt']))}</b>（占该层
  {float([l for l in layers if l['layer']=='cat_prior'][0]['waste_amt']) / float([l for l in layers if l['layer']=='cat_prior'][0]['pool_amt']) * 100:.0f}%）。
  <b>买贵的、买大件的，踩坑代价远高于零星小钱</b>——这是这份数据最实用的一条结论。
</div>

<h2>二、钱浪费在哪</h2>

<table>
<tr><th>品类</th><th>池金额</th><th>浪费金额</th><th>浪费率</th><th>占浪费总量</th><th>该品类判定依据</th><th>大额闲置</th></tr>
{''.join(cat_row(c) for c in cats)}
<tr style="background:#f2f4f7;font-weight:700">
  <td>合计</td><td class="num">{money(pool)}</td>
  <td class="num" style="color:var(--red)">{money(waste)}</td>
  <td class="num">{rate:.1f}%</td><td>100%</td><td>—</td>
  <td>{len(idle_bigs)}/{len(bigs)}</td></tr>
</table>

<div class="box blue">
  <span class="t">读法</span>
  <b>电商综合</b>浪费金额最高（¥{money(float(cats[0]['waste_amt']))}），但它的池子也最大——
  真正该盯的是<b>浪费率</b>。服饰美妆的池子只有 ¥3,935.85，却贡献了 ¥{money(float([c for c in cats if c['code']=='APPAREL'][0]['waste_amt']))} 的浪费，
  且 8 笔大额里有 2 笔确认闲置，是<b>单笔金额不高、但踩坑频率最高</b>的品类。
</div>

<h2>三、逐笔确认的大额闲置（{len(idle_bigs)} 笔 · ¥{money(idle_big_amt)}）</h2>

<p>这些不是推算，是逐笔问出来的真值。{len(idle_bigs)} 笔闲置金额占了全部浪费的
<b>{idle_big_amt / waste * 100:.0f}%</b>——<b>大额笔是浪费的主战场</b>，
而它们往往买的时候都"觉得会用得上"。</p>

<div class="scroll">
<table>
<tr><th>交易号</th><th>日期</th><th>商品</th><th>品类</th><th>金额</th><th>判定</th></tr>
{idle_big_rows}
</table>
</div>

<h3>其余大额笔（判定为在用）</h3>
<div class="scroll">
<table>
<tr><th>交易号</th><th>日期</th><th>商品</th><th>品类</th><th>金额</th><th>判定</th></tr>
{other_big_rows}
</table>
</div>

<h2>四、这套标签能用来干什么</h2>

<ul>
<li><b>训练预警模型</b>：<code>v_training_set</code> + 本报告的标注，
    特征是"下单时间、金额、是否深夜、是否促销期、品类、复购间隔"，
    标签是 <code>is_idle</code>。给用户在下单前打一个"这笔大概率会闲置"的分。</li>
<li><b>回填预警闭环</b>：<code>t_risk_record</code> 记录预警，
    事后用这份标注回填"预警对不对"，构成评估闭环。</li>
<li><b>分层加权</b>：<code>cat_prior</code> 层是先验不是真值，
    建模时要按 <code>v_stratum_weight</code> 加权，或只用 <code>big_item</code> 层做验证集。</li>
</ul>

<div class="box amber">
  <span class="t">这份数据的两条使用边界（答辩会被问）</span>
  <b>1. 品类级结论不能当逐笔事实。</b>「服饰美妆 10% 闲置」是"这类东西我大约有 10% 白买了"，
  不是"每一笔服饰都有 10% 浪费"。它适合做<b>总体估算</b>和<b>建模先验</b>，不适合训练逐笔分类器。<br>
  <b>2. 单用户样本，不能推及全体。</b>所有结论的适用范围是"这一个大学生、这两年"。
  方法可迁移（换份账单重跑即可），结论不可外推。
</div>

<h2>五、可复现性</h2>

<table>
<tr><th>环节</th><th>实现</th></tr>
<tr><td>候选池口径</td><td>视图 <code>v_idle_candidate</code>（唯一来源，应用层不重写 WHERE）</td></tr>
<tr><td>体检页生成</td><td><code>scripts/gen_waste_checkup.py</code></td></tr>
<tr><td>结论回传与核对</td><td><code>scripts/gen_result_import.py</code> →「体检结果导入」页</td></tr>
<tr><td>落库</td><td><code>scripts/import_checkup_result.py</code>（幂等，唯一键 UPSERT）</td></tr>
<tr><td>汇总视图</td><td><code>v_checkup_summary</code> / <code>v_checkup_total</code></td></tr>
<tr><td>本报告</td><td>本文档，数据由视图实时生成</td></tr>
</table>

<footer>
  数据口径：v_idle_candidate（{tot['labeled_n'] if 'labeled_n' in tot else tot['big_n'] + tot['cat_n']} 笔全标注） ·
  支出基准 3783 条记录 / 2516 笔有效支出 / ¥83,094.45<br>
  生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}
</footer>

</div>
</body>
</html>
"""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成 {OUT}")
    print(f"  池 ¥{money(pool)} · 浪费 ¥{money(waste)} ({rate:.2f}%) · 大额闲置 {len(idle_bigs)} 笔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
