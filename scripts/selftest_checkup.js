/**
 * 浪费体检页 · 离线自检（不需要浏览器）
 * ---------------------------------------------------------------
 * 用法：
 *     node scripts/selftest_checkup.js "F:/work Buddy/…/浪费体检.html"
 *
 * 为什么也要给它配一份：这页是"把前端逻辑内嵌进一个 HTML"的同一种形态，
 * 生成器里 SQL / Python / JS 三处字段名只要有一处对不上，页面不报错，
 * 只会悄悄算出错误金额。上个工具（盘点工作台）就因为 `r.id` 恒为 undefined
 * 让 334 行共用一个状态，用户改一行全表跟着变，全程零报错。
 *
 * 本页的状态是「品类码 → 比例」「交易号 → 0/1/2」，同样怕键对不上，
 * 所以断言三件事：① 键集合与内嵌数据一致 ② 改一项只影响一项 ③ 导出的
 * SQL/CSV 落到真实键上。生成后跑一遍即可。
 */
const fs = require("fs");
const vm = require("vm");

const file = process.argv[2];
if (!file) {
  console.error("用法：node scripts/selftest_checkup.js <浪费体检.html>");
  process.exit(2);
}
const html = fs.readFileSync(file, "utf8");
const js = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

let failed = 0;
const ok = (cond, label, extra = "") => {
  console.log(`${cond ? "  OK  " : "  !!  "}${label}${extra ? "  " + extra : ""}`);
  if (!cond) failed++;
};

// ---- 最小 DOM 打桩：querySelector 会缓存，便于反读写入结果；
//      querySelectorAll 返回空数组（refresh 里的遍历空转即可，calc 不碰 DOM）----
const els = {}, handlers = {};
let blobText = null;
function mk(id) {
  return {
    id, textContent: "", innerHTML: "", value: "", checked: false,
    style: { cssText: "" }, dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute() {}, removeAttribute() {}, setSelectionRange() {},
    insertAdjacentHTML() {}, addEventListener() {}, appendChild() {}, removeChild() {},
    showModal() {}, close() {}, select() {}, focus() {}, click() {},
    querySelector() { return mk("inner"); }, querySelectorAll() { return []; }
  };
}
const q = sel => (els[sel] = els[sel] || mk(sel));
const ctx = {
  document: {
    querySelector: q,
    querySelectorAll: () => [],
    addEventListener(t, f) { handlers[t] = f; },
    createElement: () => mk("a"),
    body: { appendChild() {}, removeChild() {} },
    execCommand() { return true; }
  },
  localStorage: {
    _d: {}, getItem(k) { return this._d[k] || null; },
    setItem(k, v) { this._d[k] = v; }, removeItem(k) { delete this._d[k]; }
  },
  // 剪贴板：故意让它缺 writeText，逼代码走 execCommand 回退路径
  navigator: {},
  window: {}, console,
  Blob: function (p) { blobText = p.join(""); },
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  confirm: () => true, setTimeout() {}
};
vm.createContext(ctx);
vm.runInContext(js, ctx);

const META = vm.runInContext("META", ctx);
const run = code => vm.runInContext(code, ctx);
const kpi = id => els[id] ? els[id].textContent : "";

// 造一个假按钮，模拟真实点击冒泡到 delegated handler
function fakeBtn(attrs, inTbody) {
  const self = {
    classList: { contains: c => c === (attrs["_cls"] || ""), toggle() {}, add() {}, remove() {} },
    getAttribute: k => attrs[k],
    hasAttribute: k => !(attrs[k] === undefined || attrs[k] === null),
    closest(sel) { return sel === "button" ? self : (inTbody && sel === "#tb" ? {} : null); }
  };
  return self;
}

console.log(`自检 ${file}`);
console.log(`品类 ${Object.keys(META.cat).length} 个 · 大额笔 ${Object.keys(META.big).length} 笔\n`);

// ---- 1. 键集合必须是真实存在的键，不能有 undefined ----
const catKeys = Object.keys(META.cat);
const bigKeys = Object.keys(META.big);
ok(catKeys.length > 0 && catKeys.every(k => k && k !== "undefined"), "品类键都有效", catKeys.join(","));
ok(bigKeys.length > 0 && bigKeys.every(k => /^\d+$/.test(k)), "大额笔键都是交易号",
   `首 ${bigKeys[0]}`);
ok(!/data-id="undefined"/.test(html), '页面里没有 data-id="undefined"');
ok(catKeys.every(k => typeof META.cat[k].amt === "number" && META.cat[k].amt > 0),
   "每个品类的金额都是正数");
ok(bigKeys.every(k => typeof META.big[k].amt === "number" && META.big[k].amt > 0),
   "每笔大额金额都是正数");

// ---- 2. 页面上的卡片数与内嵌数据一致（防生成器漏渲染）----
const nCatCard = (html.match(/class="card cat"/g) || []).length;
ok(nCatCard === catKeys.length, "品类卡片数 = 内嵌品类数", `${nCatCard}/${catKeys.length}`);
const nBigRow = (html.match(/<tr data-id="/g) || []).length;
ok(nBigRow === bigKeys.length, "大额行数 = 内嵌大额笔数", `${nBigRow}/${bigKeys.length}`);

// ---- 3. 冷启动必须是"一项都没选" ----
ok(kpi("#k-waste") === "¥0", "刚打开：预计浪费 ¥0", kpi("#k-waste"));
ok(kpi("#k-prog") === "0/" + (catKeys.length + bigKeys.length),
   "刚打开：进度 0/N", kpi("#k-prog"));

// ---- 4. 只点一个品类，只能影响那一个品类（静默串味的核心回归点）----
const c0 = catKeys[0], c1 = catKeys[1];
handlers["click"]({ target: fakeBtn({ "data-cat": c0, "data-pct": "0.3", "_cls": "lv" }) });
const want0 = "¥" + Math.round(META.cat[c0].amt * 0.3).toLocaleString("en-US");
ok(kpi("#k-waste") === want0, `只给「${META.cat[c0].name}」选 30% → 浪费 = 该类 30%`,
   `${kpi("#k-waste")} / 期望 ${want0}`);
ok(kpi("#k-prog") === "1/" + (catKeys.length + bigKeys.length), "进度只 +1", kpi("#k-prog"));

handlers["click"]({ target: fakeBtn({ "data-cat": c1, "data-pct": "0.1", "_cls": "lv" }) });
const want1 = "¥" + Math.round(META.cat[c0].amt * 0.3 + META.cat[c1].amt * 0.1).toLocaleString("en-US");
ok(kpi("#k-waste") === want1, "再给第二个品类选 10% → 两段相加", kpi("#k-waste"));
ok(kpi("#k-prog") === "2/" + (catKeys.length + bigKeys.length), "进度 +2", kpi("#k-prog"));

// ---- 5. 只点一笔大额，只能影响那一笔 ----
const b0 = bigKeys[0];
handlers["click"]({ target: fakeBtn({ "data-id": b0, "data-v": "1" }, true) });
const wantBig = "¥" + Math.round(
  META.cat[c0].amt * 0.3 + META.cat[c1].amt * 0.1 + META.big[b0].amt).toLocaleString("en-US");
ok(kpi("#k-waste") === wantBig, `只把「${META.big[b0].name.slice(0, 12)}」标闲置 → 加上那一笔`,
   `${kpi("#k-waste")} / 期望 ${wantBig}`);
ok(kpi("#k-prog") === "3/" + (catKeys.length + bigKeys.length), "进度 +3", kpi("#k-prog"));

// ---- 6. "说不清"不计入浪费 ----
const b1 = bigKeys[1];
handlers["click"]({ target: fakeBtn({ "data-id": b1, "data-v": "2" }, true) });
ok(kpi("#k-waste") === wantBig, "标「说不清」不增加浪费金额", kpi("#k-waste"));

// ---- 7. 一键填充 ----
run("autoFill('0.1')");
const wantAll = "¥" + Math.round(
  catKeys.reduce((a, k) => a + META.cat[k].amt * 0.1, 0) + META.big[b0].amt).toLocaleString("en-US");
ok(kpi("#k-waste") === wantAll, "一键全部 10% → 全部品类按 10% 计（大额已确认项保留）",
   `${kpi("#k-waste")} / 期望 ${wantAll}`);

// ---- 8. 导出的 SQL 必须落到真实键上 ----
if (els["#btn-sql"]) els["#btn-sql"].onclick();
const sql = els["#dlg-body"] ? els["#dlg-body"].value : "";
ok(sql.length > 0, "导出 SQL 非空");
ok(!/undefined/.test(sql), "导出 SQL 里没有 undefined");
ok(sql.indexOf("('" + b0 + "'") < 0 && new RegExp("VALUES \\(" + b0 + ",").test(sql),
   "SQL 大额段落到真实交易号", b0);
ok(sql.indexOf("category_code = '" + c0 + "'") >= 0, "SQL 品类段带到正确品类码", c0);

// ---- 9. 导出的 CSV 汇总金额要与页面一致 ----
if (els["#btn-csv"]) els["#btn-csv"].onclick();
const csv = String(blobText || "");
// 页面 KPI 四舍五入到元（¥1027），CSV 保留两位（1026.98）——两者都对，
// 这里比的是原始精度，必须用同一套浮点累加顺序。
const wasteRaw = catKeys.reduce((a, k) => a + META.cat[k].amt * 0.1, 0) + META.big[b0].amt;
ok(csv.indexOf(wasteRaw.toFixed(2)) >= 0, "CSV 汇总金额与页面一致", wasteRaw.toFixed(2));
ok(csv.split("\r\n").length > 10, "CSV 有内容（含品类段 + 大额段）",
   `${csv.split("\r\n").length} 行`);

// ---- 10. 导出兜底：下载被沙箱拦掉时，内容要能通过弹窗拿到 ----
// 用户真实踩过的坑：预览面板是沙箱 iframe，`a.download` 会被静默拦掉，
// 表现为"点了导出没反应、不报错"。所以导出必须有一条不依赖下载的出路。
ok(els["#btn-text"] !== undefined, "存在「只复制文本」按钮（不依赖下载）");
els["#dlg-body"].value = "";
els["#btn-text"].onclick();
const dlgText = els["#dlg-body"].value;
ok(csv.length > 0 || dlgText.length > 0, "下载失败时弹窗里能拿到同样内容",
   dlgText ? `${dlgText.length} 字符` : "(走了 execCommand 复制)");
if (dlgText) {
  ok(dlgText === csv, "弹窗内容与 CSV 完全一致（不是两份各自拼的）");
  ok(dlgText.indexOf(String(bigKeys[0])) >= 0, "弹窗内容带真实交易号");
}

console.log(failed ? `\n✗ 有 ${failed} 项未通过` : "\n✓ 全部通过");
process.exit(failed ? 1 : 0);
