"""账单入库管道：文件 → RawRecord → t_transaction。

一次导入的完整流程：
    1. 解析文件（各来源自己的解析器）
    2. 用文件 MD5 在 t_import_batch 登记批次（同文件不可能重复导入）
    3. 逐条分类（规则命中）
    4. 批量 INSERT IGNORE —— 靠 uk_trans_dedup 做幂等，重复行自动跳过
    5. 回填批次的统计数字（解析数 / 入库数 / 跳过数）

统计口径：
    total_rows    = 解析出的记录数
    inserted_rows = 真正写进库的
    skipped_rows  = total - inserted
skip 的构成有三种，都是正常的：
    · 重复（同一笔在多个文件里出现，或文件重导）
    · 退款/关闭行被标记后按业务规则排除的统计口径（行本身仍在库里，只是 is_refund=1）
    · 交易关闭行在解析阶段就被丢弃，不进入 total_rows
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from . import PARSERS
from .base import RawRecord, file_md5
from ..classifier import Classifier


@dataclass
class ImportResult:
    source: str
    file_name: str
    parsed: int = 0
    inserted: int = 0
    skipped: int = 0
    batch_id: Optional[int] = None
    detail: str = ""


def connect():
    """按 app.config 里的约定连接数据库。

    优先读环境变量，回退到本机开发默认值（与 .env.example 一致）。
    """
    import pymysql

    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "spending_waste"),
        charset="utf8mb4",
        autocommit=False,
    )


def ensure_user(conn, username: str = "demo", nickname: str = "本机用户",
                monthly_budget: float = 2000.0) -> int:
    """确保有一个用户存在，返回 user_id。"""
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM t_user WHERE username = %s", (username,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO t_user (username, password_hash, nickname, monthly_budget) "
            "VALUES (%s, %s, %s, %s)",
            (username, "!", nickname, monthly_budget),
        )
    return cur.lastrowid


def register_batch(conn, user_id: int, source: str, path: str,
                   encoding: str, total: int) -> Optional[int]:
    """登记导入批次。同一文件（MD5 相同）已导过则返回 None。"""
    md5 = file_md5(path)
    fname = os.path.basename(path)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT batch_id FROM t_import_batch WHERE user_id = %s AND file_md5 = %s",
            (user_id, md5),
        )
        if cur.fetchone():
            return None
        cur.execute(
            "INSERT INTO t_import_batch "
            "(user_id, source, file_name, file_md5, encoding, total_rows) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, source, fname, md5, encoding, total),
        )
        return cur.lastrowid


def import_file(conn, path: str, source: str, user_id: int,
                classifier: Classifier, dry_run: bool = False) -> ImportResult:
    """导入单个文件。"""
    parser = PARSERS[source]
    recs: List[RawRecord] = parser(path)
    res = ImportResult(source=source, file_name=os.path.basename(path), parsed=len(recs))

    encoding = "gb18030" if source == "alipay" else "utf-8"

    if dry_run:
        res.detail = "dry-run，未写库"
        return res

    batch_id = register_batch(conn, user_id, source, path, encoding, len(recs))
    if batch_id is None:
        res.detail = "该文件已导入过（MD5 相同），跳过"
        res.skipped = len(recs)
        return res
    res.batch_id = batch_id

    sql = (
        "INSERT IGNORE INTO t_transaction "
        "(user_id, batch_id, trade_no, dedup_key, trade_time, counterparty, "
        " product_desc, direction, amount, pay_method, trade_status, "
        " category_id, category_src, is_refund, is_primary, remark) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )

    rows = []
    for r in recs:
        cid, csrc = classifier.classify(r.counterparty, r.product_desc, r.direction)
        rows.append((
            user_id, batch_id, r.trade_no, r.dedup_key, r.trade_time,
            r.counterparty or None, r.product_desc or None, r.direction,
            r.amount, r.pay_method or None, r.trade_status or None,
            cid, csrc, 1 if r.is_refund else 0, 1 if r.is_primary else 0,
            r.remark or None,
        ))

    inserted = 0
    with conn.cursor() as cur:
        # 分批提交，避免一次 500+ 行的长事务
        for i in range(0, len(rows), 200):
            chunk = rows[i:i + 200]
            cur.executemany(sql, chunk)
            inserted += cur.rowcount

    res.inserted = inserted
    res.skipped = len(recs) - inserted

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE t_import_batch SET inserted_rows=%s, skipped_rows=%s "
            "WHERE batch_id=%s",
            (res.inserted, res.skipped, batch_id),
        )
    conn.commit()
    return res
