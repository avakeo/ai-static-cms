"""生成・公開の実行ログ。管理UIの「実行ログ」が読む。

失敗も行として残す。公開が止まったことに、読者より先に気づくため。
"""

from __future__ import annotations

import sqlite3

from cms.article_queue import now_utc

STATUSES = ("success", "error", "skipped")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            used_account TEXT NOT NULL DEFAULT '',
            total_tokens INTEGER,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def record(db_path: str, mode: str, status: str, *, title: str = "", filename: str = "",
           used_account: str = None, usage: dict = None, error: str = "") -> None:
    with sqlite3.connect(db_path) as conn:
        ensure_table(conn)
        conn.execute(
            "INSERT INTO post_log (mode, status, title, filename, used_account,"
            " total_tokens, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, status, title, filename, used_account or "",
             (usage or {}).get("total"), error, now_utc()),
        )


def list_logs(db_path: str, mode: str = None, status: str = None, limit: int = 100) -> list[dict]:
    where, params = [], []
    if mode:
        where.append("mode = ?")
        params.append(mode)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = ("SELECT id, mode, status, title, filename, used_account, total_tokens,"
           " error, created_at FROM post_log")
    if where:
        sql += " WHERE " + " AND ".join(where)
    with sqlite3.connect(db_path) as conn:
        ensure_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in rows]
