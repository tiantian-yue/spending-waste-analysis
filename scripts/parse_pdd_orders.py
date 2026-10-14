# -*- coding: utf-8 -*-
"""从「拼多多订单页 · 页面原文」快照中还原订单/商品清单。

为什么需要这个脚本
------------------
拼多多网页版订单列表页（mobile.yangkeduo.com/orders.html）**不展示订单号**、
**也没有下单日期**，卡片上只有：店铺名 / 状态 / 商品标题 / 规格 / 原价 / 实付金额。
因此任何以「订单号」为锚点的 DOM 提取都会 0 命中（本项目实测：4 次运行全部如此）。

页面里混着两种卡片版式，且 innerText 会把同一张卡片的若干字段**挤成一行**：
    A 商城订单：店铺 / 状态 / 标题 / ×1，规格 / 标签 / [￥原价] / 实付 / ￥实付 / (免运费) / 按钮
    B 多多买菜：多多买菜 / 状态 / 规格 | 标题 / ￥原价 / ×1 / 共N件商品实付:￥实付(免运费) / 按钮

所以流程是：① 行归一化（把挤在一起的字段拆开）→ ② 以「实付」为锚点切卡片
→ ③ 以「状态行」反推卡片起点（比猜店铺名可靠）→ ④ 跨快照去重。

用法
----
    python scripts/parse_pdd_orders.py "F:/.../拼多多页面原文*.txt" -o pdd_orders.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import os
import re
import sys
from collections import Counter

# ---------------------------------------------------------------- 词表

AMT_RE = re.compile(r"^[￥¥]\s*([\d,]+(?:\.\d{1,2})?)\s*$")
SPEC_RE = re.compile(r"^[×xX*＊]\s*\d+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SNAP_MARK = re.compile(r"^=+\s*快照?\s*\d*\s*=*$|^\d+\s*=+$")

STATUS_WORDS = (
    "待付款", "待支付", "待分享", "待发货", "待收货", "待评价", "已评价",
    "交易成功", "已签收，待确认收货", "已签收", "已提货", "已取货", "已完成",
    "拼单成功", "待成团", "已取消", "交易关闭", "退款成功", "已退款", "退款中",
)
REFUND_WORDS = ("退款成功", "已退款", "交易关闭", "已取消")

BUTTON_RE = re.compile(
    r"^(确认收货|查看物流|申请退款|更多|立即评价|再次拼单|追加评价|申请发票|查看详情|"
    r"联系客服|删除订单|去支付|立即支付|取消订单|合并支付|申请售后|再次购买|去评价|"
    r"评价返现|售后详情|修改地址|催发货|提醒发货|服务详情|查看评价|邀请拼单|"
    r"去评价晒单|晒单评价|再次下单|查看同款|加入购物车)$"
)

TAG_KEYWORDS = (
    "包邮", "保障中", "价保", "返现", "送达", "补贴", "赔", "退货", "免运费",
    "极速退款", "正品", "先用后付", "秒杀", "满减", "运费险", "无理由", "效期",
    "件商品", "已优惠", "官方立减", "限时", "预售", "定金", "券后", "立减",
    "折扣", "承诺", "包退", "保修", "保价", "新品", "热销", "特惠", "直降",
    "秒发", "闪电", "包换", "假一赔", "品质", "坏了", "破损",
)

SHOP_END_RE = re.compile(
    r"([\u4e00-\u9fff]{2,}[_\-]\d{1,8}|店|百货|商贸|超市|大药房|药房|药店|书城|书店|"
    r"旗舰|专营|专卖|自营|优选|甄选|食品|生鲜|数码|服饰|鞋业|商行|商社|铺子|"
    r"小店|买菜|快印|图文|家居|母婴|宠物|官方|菜场|果园|农场)(?:[_\-]?\d{0,8})$"
)

# 拼多多页面上的 UI 标签，不是店铺名也不是商品名
LABELS = {
    "钱款去向", "免运费", "已优惠", "查看更多", "展开", "收起", "优惠明细",
    "官方客服", "全部", "待付款", "待分享", "待发货", "待收货", "评价",
    "搜索订单", "订单详情", "查看物流信息", "交易快照",
}

# 状态行：拼多多的状态文案五花八门（订单已取消，退款成功 / 换货/补寄成功 / 已提货…），
# 枚举不完，改用「以状态类词开头 + 短」的启发式判据。
STATUS_START_RE = re.compile(r"^(待|已|未|交易|拼单|退款|换货|售后|订单|部分|全部|拒绝|补寄|团|拼)")

HEADER_NOISE = ("用手机浏览器扫码", "搜索订单", "正在加载中", "加载中", "没有更多", "暂无更多")

# 归一化：把被 innerText 挤在一行的字段拆回独立行
SPLIT_PATTERNS = (
    re.compile(r"\s*(共\s*\d+\s*件商品)\s*"),
    re.compile(r"\s*(先用后付)\s*"),
    re.compile(r"\s*(实付[:：]?)"),
    re.compile(r"\s*([（(]\s*免运费\s*[)）])\s*"),
    re.compile(r"\s*([￥¥]\s*[\d,]+(?:\.\d{1,2})?)\s*"),
)


def normalize(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        for pat in SPLIT_PATTERNS:
            s = pat.sub(lambda m: "\n" + m.group(1) + "\n", s)
        for piece in s.split("\n"):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


# ---------------------------------------------------------------- 判据

def is_shop(line: str, require_suffix: bool = True) -> bool:
    """像店铺名吗？

    require_suffix=True  —— 必须命中「…店 / 商贸 / 生鲜 / 旗舰」之类后缀（严格）
    require_suffix=False —— 只排除噪声，用于「状态行前一行 = 店铺名」这个结构判据
                            （拼多多店铺名结尾五花八门：男装、零食铺、茶集74_898949…）
    """
    if not (3 <= len(line) <= 34):
        return False
    if line in LABELS or is_tag(line) or is_status(line):
        return False
    if BUTTON_RE.match(line) or AMT_RE.match(line) or SPEC_RE.match(line):
        return False
    if not CJK_RE.search(line):
        return False
    if any(k in line for k in ("快递", "签收", "订单", "退货", "退款", "券", "￥", "¥", "×")):
        return False
    if require_suffix:
        return bool(SHOP_END_RE.search(line))
    return True


def card_start(seg: list[str]) -> int | None:
    """返回卡片起点下标（店铺名那一行）。

    判据一（主力）：段内第一条「状态行」的前一行 —— 拼多多卡片一律是 店铺名 → 状态 → 商品。
    判据二（兜底）：段内最后一条带店铺后缀的行。
    """
    for i, l in enumerate(seg):
        if i > 0 and is_status(l) and is_shop(seg[i - 1], require_suffix=False):
            return i - 1
    for i in range(len(seg) - 1, -1, -1):
        if is_shop(seg[i]):
            return i
    return None


def is_noise(line: str) -> bool:
    if any(line.startswith(k) for k in HEADER_NOISE):
        return True
    if SNAP_MARK.match(line) or line.startswith("#"):
        return True
    if len(line) > 120:
        return True
    if "快递" in line and len(line) > 36:
        return True
    return False


def is_tag(line: str) -> bool:
    if line.startswith("(") and line.endswith(")"):
        return True
    if len(line) > 20:
        return False
    return any(k in line for k in TAG_KEYWORDS)


def is_status(line: str) -> bool:
    if line in STATUS_WORDS:
        return True
    if is_tag(line):
        return False
    if len(line) > 22 or "×" in line or "￥" in line or "¥" in line:
        return False
    if len(line) < 2:
        return False
    return bool(STATUS_START_RE.match(line))


def is_refunded(status: str, item_flag: bool) -> bool:
    if item_flag:
        return True
    return any(k in status for k in ("退款", "取消", "关闭", "失败"))


def is_title_like(line: str) -> bool:
    if len(line) < 4 or len(line) > 110:
        return False
    if line in LABELS:
        return False
    if not CJK_RE.search(line):
        return False
    if is_tag(line) or is_status(line) or BUTTON_RE.match(line):
        return False
    if AMT_RE.match(line) or SPEC_RE.match(line):
        return False
    return True


# ---------------------------------------------------------------- 解析

def parse_snapshot(text: str) -> list[dict]:
    """把一张快照（整页 innerText）切成若干「商品行」。"""
    lines = [l for l in normalize(text) if not is_noise(l)]
    anchors = [i for i, l in enumerate(lines) if l.startswith("实付") or "实付" in l and len(l) <= 14]
    out: list[dict] = []
    prev_end = 0

    for i in anchors:
        amt, amt_idx = None, i
        for j in range(i + 1, min(i + 5, len(lines))):
            m = AMT_RE.match(lines[j])
            if m:
                amt = float(m.group(1).replace(",", ""))
                amt_idx = j
                break
        seg = lines[prev_end:i]
        prev_end = amt_idx + 1
        if not seg or amt is None:
            continue

        # 卡片起点 = 店铺名
        si = card_start(seg)
        shop, card = (seg[si], seg[si:]) if si is not None else ("", seg)

        # 逐行扫描卡片，遇到新标题就新起一件商品
        items: list[dict] = []
        pending: dict | None = None
        for l in card[1:] if shop else card:
            if is_status(l):
                if l in REFUND_WORDS and pending is not None:
                    pending["item_refund"] = True
                continue
            if BUTTON_RE.match(l) or is_tag(l):
                continue
            m = AMT_RE.match(l)
            if m:
                if pending is not None and pending["price"] is None:
                    pending["price"] = float(m.group(1).replace(",", ""))
                continue
            if SPEC_RE.match(l):
                if pending is not None and not pending["spec"]:
                    pending["spec"] = l
                continue
            if len(l) < 4 or not CJK_RE.search(l) or len(l) > 110:
                continue
            title, spec_hint = l, ""
            if " | " in l:
                a, b = l.split(" | ", 1)
                if b.strip():
                    spec_hint, title = a.strip(), b.strip()
            if not is_title_like(title):
                continue
            pending = {"title": title, "spec": spec_hint, "price": None, "item_refund": False}
            items.append(pending)

        card_status = next((l for l in card if is_status(l)), "")
        # 卡片里出现「先用后付」标签 = 这单是先用后付付的 —— 匹配账单时的重要线索
        pay = "先用后付" if any("先用后付" in l for l in card) else ""
        for it in items:
            out.append({
                "shop": shop, "status": card_status, "title": it["title"],
                "spec": it["spec"], "price": it["price"], "amount": amt,
                "refund": is_refunded(card_status, it["item_refund"]),
                "pay": pay,
            })
    return out


def parse_files(patterns: list[str]) -> tuple[list[dict], dict]:
    files: list[str] = []
    for p in patterns:
        files.extend(sorted(glob.glob(p)))
    files = sorted(set(files))
    if not files:
        raise SystemExit("没有匹配到任何「拼多多页面原文*.txt」文件")

    counter: Counter = Counter()
    meta: dict[tuple, dict] = {}
    seq = 0
    n_snap = 0
    for f in files:
        raw = io.open(f, encoding="utf-8", errors="replace").read()
        body = raw.split("\n\n", 1)[1] if raw.startswith("#") else raw
        for chunk in body.split("===== 快照"):
            if len(chunk.strip()) < 40:
                continue
            n_snap += 1
            for rec in parse_snapshot(chunk):
                key = (rec["shop"], rec["title"], rec["amount"])
                counter[key] += 1
                if key not in meta:
                    # seq = 首次出现次序。快照是逐轮累积的，首次出现越早 = 列表越靠前 = 越新
                    rec["seq"] = seq
                    seq += 1
                    meta[key] = rec
                else:
                    # 后续快照若标出「先用后付」或「退款」，补记（后出现的状态更权威）
                    if rec.get("pay"):
                        meta[key]["pay"] = rec["pay"]
                    if rec["refund"]:
                        meta[key]["refund"] = True
    rows = []
    for key, c in counter.most_common():
        r = dict(meta[key])
        r["seen"] = c
        rows.append(r)
    rows.sort(key=lambda r: r.get("seq", 10 ** 9))   # 按页面顺序（新→旧）排列，便于人工核对
    return rows, {"files": files, "snapshots": n_snap}


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("patterns", nargs="+", help="原文 txt 的路径或通配符")
    ap.add_argument("-o", "--out", default="pdd_orders.csv")
    args = ap.parse_args()

    rows, stats = parse_files(args.patterns)
    print(f"读取文件 {len(stats['files'])} 个 · 快照 {stats['snapshots']} 张")
    for f in stats["files"]:
        print("   ", os.path.basename(f))
    n_refund = sum(1 for r in rows if r["refund"])
    print(f"还原出「商品行」{len(rows)} 条（跨快照去重后），其中退款/关闭 {n_refund} 条")
    print(f"店铺识别成功 {sum(1 for r in rows if r['shop'])} 条 / 空店铺 {sum(1 for r in rows if not r['shop'])} 条")

    with io.open(args.out, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["次序", "店铺", "状态", "商品标题", "规格", "原价", "实付",
                    "支付方式", "退款", "出现次数"])
        for r in rows:
            w.writerow([r.get("seq", ""), r["shop"], r["status"], r["title"], r["spec"],
                        "" if r["price"] is None else f'{r["price"]:.2f}',
                        f'{r["amount"]:.2f}', r.get("pay", ""),
                        "1" if r["refund"] else "",
                        r["seen"]])

    amt_cnt = Counter(f'{r["amount"]:.2f}' for r in rows if not r["refund"])
    print(f"\n不同实付金额 {len(amt_cnt)} 种 · 商品行 {len(rows)} 条")
    print("金额 Top10：", "、".join(f"¥{a}×{c}" for a, c in amt_cnt.most_common(10)))
    print(f"\n输出 → {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
