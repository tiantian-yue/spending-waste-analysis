/**
 * 闲置盘点工作台 · 离线自检（不需要浏览器）
 * ---------------------------------------------------------------
 * 用法：
 *     node scripts/selftest_workbench.js "F:/work Buddy/…/闲置盘点工作台.html"
 *
 * 为什么要它：工作台是"把整个前端逻辑内嵌进一个 HTML"的形态，
 * 生成器里 SQL / Python / JS 三处字段名只要有一处对不上，
 * 页面不会报错，只会**悄悄算出错误结果**。踩过的真实事故：
 *
 *   SQL 查出来叫 trans_id，JS 通篇写 r.id → r.id 恒为 undefined
 *   → 334 行共用同一份 state（localStorage 里只有 "undefined" 一个键）
 *   → 用户改任意一行，触发一次 render，全表一起变成"已移出/已判定"
 *   → 导出的 SQL 是 `WHERE trans_id=undefined`，一句报错都没有
 *
 * 本脚本把这类"静默错位"变成一个会退出的断言，生成后跑一遍即可。
 */
const fs = require("fs");
const vm = require("vm");

const file = process.argv[2];
if (!file) {
  console.error("用法：node scripts/selftest_workbench.js <工作台.html>");
  process.exit(2);
}
const html = fs.readFileSync(file, "utf8");
const js = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

let failed = 0;
const ok = (cond, label, extra = "") => {
  console.log(`${cond ? "  OK  " : "  !!  "}${label}${extra ? "  " + extra : ""}`);
  if (!cond) failed++;
};

// ---- 最小 DOM 打桩：只要能跑通页面里的 render / 事件处理就够了 ----
const els = {}, handlers = {};
let blobText = null;
function mk(id) {
  return { id, textContent: "", innerHTML: "", value: "", checked: false, style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    insertAdjacentHTML() {}, addEventListener() {}, appendChild() {}, showModal() {}, close() {},
    select() {}, focus() {}, click() {}, querySelector() { return mk("inner"); } };
}
const q = sel => (els[sel] = els[sel] || mk(sel));
const ctx = {
  document: { querySelector: q, addEventListener(t, f) { handlers[t] = f; },
              createElement: () => mk("a"), execCommand() {} },
  localStorage: { _d: {}, getItem(k) { return this._d[k] || null; },
                  setItem(k, v) { this._d[k] = v; }, removeItem(k) { delete this._d[k]; } },
  window: {}, console,
  Blob: function (p) { blobText = p.join(""); },
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  confirm: () => true, setTimeout() {}
};
vm.createContext(ctx);
vm.runInContext(js, ctx);
const ROWS = vm.runInContext("ROWS", ctx);

const counts = () => ({ 移出: +els["#k-out"].textContent, 已判: +els["#k-done"].textContent });
const rerender = () => handlers["change"]({ target: mk("#f-state") });

console.log(`自检 ${file}`);
console.log(`共 ${ROWS.length} 行\n`);

// ---- 1. 每行必须有唯一、真实的 id ----
const ids = ROWS.map(r => r.id);
ok(ids.every(i => typeof i === "number" && Number.isFinite(i)),
   "每行 id 都是数字", `（首行 ${ids[0]}）`);
ok(new Set(ids).size === ids.length, "id 无重复", `${new Set(ids).size}/${ids.length}`);
ok(!/data-id="undefined"/.test(html), "页面里没有 data-id=\"undefined\"");

// ---- 2. 冷启动必须是"一行都没动" ----
const c0 = counts();
ok(c0.移出 === 0 && c0.已判 === 0, "刚打开：0 行已移出 / 0 行已判定", JSON.stringify(c0));

// ---- 3. 只改一行，只能影响一行（这是事故的核心回归点）----
handlers["change"]({ target: { dataset: { id: String(ROWS[0].id) }, value: "GROC",
                               classList: { contains: c => c === "c", toggle() {} } } });
rerender();
ok(counts().移出 === 1, "改第 1 行分类 → 仅 1 行移出", JSON.stringify(counts()));

handlers["change"]({ target: { dataset: { id: String(ROWS[1].id) }, value: "1",
                               classList: { contains: c => c === "i", toggle() {} } } });
rerender();
ok(counts().已判 === 2, "再标第 2 行闲置 → 累计 2 行已判定", JSON.stringify(counts()));

// ---- 4. 导出的 SQL 必须落到真实交易号上 ----
els["#btn-sql"].onclick();
const sql = els["#dlg-body"].value;
ok(!/undefined/.test(sql), "导出 SQL 里没有 undefined");
ok(/WHERE trans_id=\d+/.test(sql), "导出 SQL 带真实交易号",
   (sql.match(/WHERE trans_id=\d+/) || [""])[0]);

// ---- 5. 导出的 CSV 第一列必须是交易号 ----
els["#btn-csv"].onclick();
const firstCell = String(blobText).split("\r\n")[1].split(",")[0];
ok(/^\d+$/.test(firstCell), "导出 CSV 首列是交易号", firstCell);

console.log(failed ? `\n✗ 有 ${failed} 项未通过` : "\n✓ 全部通过");
process.exit(failed ? 1 : 0);
