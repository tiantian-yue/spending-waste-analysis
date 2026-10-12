"""账单导入命令行入口。

用法（在项目根目录执行）：

    # 先空跑，只看解析结果，不写库
    python scripts/import_bills.py --dry-run

    # 正式导入 data/ 下的全部账单
    python scripts/import_bills.py

    # 只导入某一个文件
    python scripts/import_bills.py --file data/wechat_xxx.xlsx --source wechat

环境变量：DB_USER / DB_PASSWORD / DB_HOST / DB_PORT / DB_NAME
本机开发默认 root / 空密码 / 127.0.0.1 / 3306 / spending_waste
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.classifier import Classifier                      # noqa: E402
from app.services.importers import pipeline                          # noqa: E402
from app.services.importers.base import file_md5                     # noqa: E402

# 文件名特征 → 来源。放在这里而不是靠用户每次手输，减少出错。
SOURCE_HINTS = (
    ("微信", "wechat"),
    ("wechat", "wechat"),
    ("支付宝", "alipay"),
    ("alipay", "alipay"),
    ("工商", "bank"),
    ("icbc", "bank"),
    ("银行", "bank"),
)


def guess_source(name: str) -> str | None:
    low = name.lower()
    for kw, src in SOURCE_HINTS:
        if kw.lower() in low:
            return src
    return None


def collect_files(data_dir: str) -> list[tuple[str, str]]:
    """扫描目录，返回 [(路径, 来源), ...]。"""
    found = []
    for fn in sorted(os.listdir(data_dir)):
        if fn.startswith("_") or fn.startswith("."):
            continue
        if not fn.lower().endswith((".xlsx", ".csv", ".pdf")):
            continue
        src = guess_source(fn)
        if src:
            found.append((os.path.join(data_dir, fn), src))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="导入微信/支付宝/工行账单到 spending_waste 库")
    ap.add_argument("--data-dir", default="data", help="账单文件所在目录")
    ap.add_argument("--file", help="只导入指定文件")
    ap.add_argument("--source", choices=["wechat", "alipay", "bank"], help="配合 --file 指定来源")
    ap.add_argument("--user", default="demo", help="归属用户名")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写库")
    args = ap.parse_args()

    if args.file:
        src = args.source or guess_source(os.path.basename(args.file))
        if not src:
            print(f"[错误] 无法从文件名判断来源，请用 --source 指定：{args.file}")
            return 2
        targets = [(args.file, src)]
    else:
        targets = collect_files(args.data_dir)

    if not targets:
        print(f"[错误] 在 {args.data_dir} 下没找到可识别的账单文件")
        return 2

    print("=" * 74)
    print(f"待导入 {len(targets)} 个文件" + ("（空跑模式，不写库）" if args.dry_run else ""))
    print("=" * 74)
    for p, s in targets:
        print(f"  [{s:6}] {os.path.basename(p)}")
    print()

    conn = None
    if not args.dry_run:
        conn = pipeline.connect()
        user_id = pipeline.ensure_user(conn, args.user)
        classifier = Classifier.from_db(conn)
        print(f"用户 ID = {user_id}，已加载分类规则 {len(classifier.rules)} 条\n")
    else:
        user_id, classifier = 0, None

    total_parsed = total_inserted = total_skipped = 0
    for path, src in targets:
        if args.dry_run:
            res = pipeline.import_file(None, path, src, 0, None, dry_run=True)
        else:
            res = pipeline.import_file(conn, path, src, user_id, classifier)
        total_parsed += res.parsed
        total_inserted += res.inserted
        total_skipped += res.skipped
        flag = res.detail or f"入库 {res.inserted} / 解析 {res.parsed} / 跳过 {res.skipped}"
        print(f"  {os.path.basename(res.file_name):52} {flag}")

    print()
    print("=" * 74)
    print(f"解析合计 {total_parsed} 条 | 入库 {total_inserted} 条 | 跳过 {total_skipped} 条")

    if conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM t_transaction WHERE user_id=%s", (user_id,))
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*), ROUND(SUM(amount),2) FROM t_transaction "
                "WHERE user_id=%s AND direction='out' AND is_refund=0 AND is_primary=1",
                (user_id,),
            )
            n, amt = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*), ROUND(SUM(amount),2) FROM t_transaction "
                "WHERE user_id=%s AND direction='out' AND is_refund=0 AND is_primary=0",
                (user_id,),
            )
            mn, mamt = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM t_transaction WHERE user_id=%s AND category_id IS NULL",
                (user_id,),
            )
            unc = cur.fetchone()[0]
            cur.execute(
                "SELECT MIN(trade_time), MAX(trade_time) FROM t_transaction WHERE user_id=%s",
                (user_id,),
            )
            lo, hi = cur.fetchone()
        print()
        print(f"  库内总记录 {total} 条，时间跨度 {lo} → {hi}")
        print(f"  计入统计的有效支出 {n} 笔，合计 {amt or 0:,.2f} 元")
        print(f"  已剔除的资金镜像行 {mn} 笔，合计 {mamt or 0:,.2f} 元（防跨源双计）")
        print(f"  未分类 {unc} 条（{unc / total * 100:.1f}%）")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
