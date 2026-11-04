"""記事キュー（公開前の記事と、取り下げた記事の置き場）。

`draft_threads` テーブルを「状態を持つ記事キュー」として扱う。バッチ生成の
下書き・予約公開・取り下げた記事を、別々の仕組みではなく1つの状態機械で表す。

    queued ──時刻を指定──→ scheduled ──時刻到来──→ published
       ↑                                              │
       └──────── 取り下げ（raw_text を戻す）───────────┘ → hidden

| 状態        | 意味                     | HTML |
|-------------|--------------------------|------|
| `queued`    | 貯めてある（時刻未指定） | 無い |
| `scheduled` | 公開時刻を指定済み       | 無い |
| `hidden`    | 公開したが取り下げた     | 無い |

`published` はこのテーブルには居ない。公開中の記事は `posted_threads` が正本で、
キューからは公開に成功した時点で消える。

**取り下げた記事の raw_text を持ち帰るのが要点。** 取り下げ時に raw_text を
キューへ戻せば、再公開は通常の公開処理をそのまま通せる。HTML を復元する
専用処理が要らない。

時刻は **UTC の "YYYY-MM-DD HH:MM:SS"** で持つ。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

STATES = ("queued", "scheduled", "hidden")

# ドリップ公開の取り出し順。
PUBLISH_ORDERS = ("fifo", "lifo", "random")

_JST = timezone(timedelta(hours=9))


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def jst_to_utc(value: str) -> str:
    """管理UIの `datetime-local`（JST）を保存用のUTC文字列にする。

    入力例: "2026-08-21T18:30"。空なら空を返す。形式が違えば ValueError。
    """
    value = (value or "").strip().replace("T", " ")
    if not value:
        return ""
    fmt = "%Y-%m-%d %H:%M:%S" if len(value) > 16 else "%Y-%m-%d %H:%M"
    dt = datetime.strptime(value, fmt).replace(tzinfo=_JST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_to_jst(value: str) -> str:
    """保存されているUTCを画面表示用のJSTにする。"""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS draft_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            used_account TEXT,
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            topic TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'queued',
            publish_at TEXT NOT NULL DEFAULT '',
            -- 取り下げた記事を戻すときに、元のファイル名を引き継いでURLを変えないため
            origin_filename TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


_COLUMNS = ("id, mode, raw_text, used_account, prompt_tokens, output_tokens, total_tokens,"
            " topic, state, publish_at, origin_filename, created_at")
_SELECT = f"SELECT {_COLUMNS} FROM draft_threads"

# 一覧に出すスレタイの取り出し。`threads_maker` の冒頭と同じ規則で、先頭の
# 区切り行を1つ読み飛ばす。AI が `^` から書き始めることがあり、素朴に1行目を
# 取ると一覧のタイトルが `^` になって中身が判別できなくなる。
_LEAD_DELIM = re.compile(r"^[ \t]*\d*[ \t]*:?[ \t]*\^[ \t]*\n")


def title_of(raw_text: str) -> str:
    """生テキストからスレタイを取り出す（表示用）。"""
    text = _LEAD_DELIM.sub("", (raw_text or "").strip(), count=1)
    return next(iter(text.strip().splitlines()), "")


def _row_to_dict(row) -> dict:
    (rid, mode, raw_text, used_account, p_tok, o_tok, t_tok, topic, state,
     publish_at, origin, created_at) = row
    return {
        "id": rid,
        "mode": mode,
        "raw_text": raw_text,
        "used_account": used_account,
        "usage": {"prompt": p_tok, "output": o_tok, "total": t_tok} if t_tok else None,
        "topic": topic or "",
        "state": state or "queued",
        "publish_at": publish_at or "",
        "origin_filename": origin or "",
        "created_at": created_at,
        "title": title_of(raw_text),
    }


def add(conn: sqlite3.Connection, mode: str, raw_text: str, *, used_account: str = None,
        usage: dict = None, topic: str = "", state: str = "queued", publish_at: str = "",
        origin_filename: str = "") -> int:
    """キューに1件積む。積んだ行のidを返す。"""
    ensure_table(conn)
    usage = usage or {}
    cur = conn.execute(
        "INSERT INTO draft_threads (mode, raw_text, used_account, prompt_tokens,"
        " output_tokens, total_tokens, topic, state, publish_at, origin_filename)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mode, raw_text, used_account, usage.get("prompt"), usage.get("output"),
         usage.get("total"), topic or "", state if state in STATES else "queued",
         publish_at or "", origin_filename or ""),
    )
    conn.commit()
    return cur.lastrowid


def list_items(conn: sqlite3.Connection, mode: str = None, state: str = None) -> list[dict]:
    """キューの中身を返す。新しい順。"""
    ensure_table(conn)
    sql, params, where = _SELECT, [], []
    if mode:
        where.append("mode = ?")
        params.append(mode)
    if state:
        where.append("state = ?")
        params.append(state)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [_row_to_dict(r) for r in conn.execute(sql + " ORDER BY id DESC", params).fetchall()]


def get(conn: sqlite3.Connection, item_id: int) -> dict | None:
    ensure_table(conn)
    row = conn.execute(_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return _row_to_dict(row) if row else None


def counts(conn: sqlite3.Connection) -> dict:
    """モード別・状態別の件数。"""
    ensure_table(conn)
    out: dict[str, dict[str, int]] = {}
    for mode, state, n in conn.execute(
            "SELECT mode, state, COUNT(*) FROM draft_threads GROUP BY mode, state"):
        out.setdefault(mode, {})[state or "queued"] = n
    return out


def schedule(conn: sqlite3.Connection, item_id: int, publish_at_utc: str) -> None:
    """公開時刻を設定する。空文字を渡すと予約を解除して queued に戻す。"""
    ensure_table(conn)
    if publish_at_utc:
        conn.execute("UPDATE draft_threads SET state = 'scheduled', publish_at = ? WHERE id = ?",
                     (publish_at_utc, item_id))
    else:
        conn.execute("UPDATE draft_threads SET state = 'queued', publish_at = '' WHERE id = ?",
                     (item_id,))
    conn.commit()


def due(conn: sqlite3.Connection, now: str = None) -> list[dict]:
    """公開時刻が来た予約を返す（古い順）。"""
    ensure_table(conn)
    rows = conn.execute(
        _SELECT + " WHERE state = 'scheduled' AND publish_at != '' AND publish_at <= ?"
        " ORDER BY publish_at", (now or now_utc(),)).fetchall()
    return [_row_to_dict(r) for r in rows]


def next_item(conn: sqlite3.Connection, mode: str, order: str = "fifo") -> dict | None:
    """次に公開する1件を返す。行は消さない（公開に成功してから `remove` する）。

    対象は `queued` のみ。予約済みは指定時刻に出すものなのでここでは拾わない。
    """
    ensure_table(conn)
    order_sql = {"fifo": "ORDER BY id", "lifo": "ORDER BY id DESC",
                 "random": "ORDER BY RANDOM()"}.get(order, "ORDER BY id")
    row = conn.execute(
        _SELECT + f" WHERE mode = ? AND state = 'queued' {order_sql} LIMIT 1", (mode,)).fetchone()
    return _row_to_dict(row) if row else None


def remove(conn: sqlite3.Connection, item_id: int) -> bool:
    ensure_table(conn)
    cur = conn.execute("DELETE FROM draft_threads WHERE id = ?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


# --- ゴミ箱 ---------------------------------------------------------------
#
# 「削除」が物理削除だと、押し間違えた記事が戻らない。行を別テーブル
# （`trashed_drafts`）へ移す。`draft_threads` に状態 `trashed` を足す案は
# 採らなかった。状態で絞っていない読み手（件数・既出のお題の判定）に、
# ゴミ箱の下書きが混ざるため。移せば今の読み手は何も変わらない。

def _ensure_trash_table(conn: sqlite3.Connection) -> list[str]:
    """`trashed_drafts` を用意し、`draft_threads` の列名を返す。"""
    ensure_table(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(draft_threads)")]
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trashed_drafts AS SELECT * FROM draft_threads WHERE 0")
    have = [r[1] for r in conn.execute("PRAGMA table_info(trashed_drafts)")]
    if "trashed_at" not in have:
        conn.execute("ALTER TABLE trashed_drafts ADD COLUMN trashed_at TEXT")
    return cols


def trash(conn: sqlite3.Connection, item_id: int) -> bool:
    """下書きをゴミ箱へ移す。"""
    cols = _ensure_trash_table(conn)
    if not conn.execute("SELECT 1 FROM draft_threads WHERE id = ?", (item_id,)).fetchone():
        return False
    quoted = ", ".join(f'"{c}"' for c in cols)
    conn.execute("DELETE FROM trashed_drafts WHERE id = ?", (item_id,))
    conn.execute(
        f"INSERT INTO trashed_drafts ({quoted}, trashed_at)"
        f" SELECT {quoted}, ? FROM draft_threads WHERE id = ?", (now_utc(), item_id))
    conn.execute("DELETE FROM draft_threads WHERE id = ?", (item_id,))
    conn.commit()
    return True


def list_trashed(conn: sqlite3.Connection, mode: str = None) -> list[dict]:
    """ゴミ箱の中身（捨てた順に新しいものから）。"""
    _ensure_trash_table(conn)
    conn.commit()
    sql = _SELECT.replace("FROM draft_threads", ", trashed_at FROM trashed_drafts")
    params = []
    if mode:
        sql += " WHERE mode = ?"
        params.append(mode)
    out = []
    for row in conn.execute(sql + " ORDER BY trashed_at DESC, id DESC", params).fetchall():
        item = _row_to_dict(row[:-1])
        item["trashed_at"] = row[-1] or ""
        out.append(item)
    return out


def restore(conn: sqlite3.Connection, item_id: int) -> str:
    """ゴミ箱の下書きをキューへ戻す。問題があればその説明、無ければ空文字。

    予約時刻を過ぎた `scheduled` は `queued` に戻す。そのまま戻すと次の予約公開で
    即公開される。戻した人が意図した動きではない。
    """
    cols = _ensure_trash_table(conn)
    row = conn.execute("SELECT state, publish_at FROM trashed_drafts WHERE id = ?",
                       (item_id,)).fetchone()
    if not row:
        return "ゴミ箱に見つかりません。"
    if conn.execute("SELECT 1 FROM draft_threads WHERE id = ?", (item_id,)).fetchone():
        return "同じ id の下書きがすでにキューにあります。"
    quoted = ", ".join(f'"{c}"' for c in cols)
    conn.execute(f"INSERT INTO draft_threads ({quoted})"
                 f" SELECT {quoted} FROM trashed_drafts WHERE id = ?", (item_id,))
    state, publish_at = row
    if state == "scheduled" and (not publish_at or publish_at <= now_utc()):
        conn.execute("UPDATE draft_threads SET state = 'queued', publish_at = '' WHERE id = ?",
                     (item_id,))
    conn.execute("DELETE FROM trashed_drafts WHERE id = ?", (item_id,))
    conn.commit()
    return ""


def purge(conn: sqlite3.Connection, item_id: int) -> bool:
    """ゴミ箱の下書きを完全に削除する。キューにある下書きは消さない。"""
    _ensure_trash_table(conn)
    cur = conn.execute("DELETE FROM trashed_drafts WHERE id = ?", (item_id,))
    conn.commit()
    return cur.rowcount > 0
