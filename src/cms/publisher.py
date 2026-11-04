"""静的サイトの出力。

`output/{mode}/` に記事ページ・一覧・RSS・サイトマップを書き出す。
書き出したディレクトリは、そのまま Cloudflare Pages などの静的ホスティングに置ける。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from html import escape
from xml.sax.saxutils import escape as xml_escape

from cms.article_queue import now_utc, utc_to_jst

STYLE = """
body{margin:0;background:#f6f5f2;color:#222;font-family:system-ui,"Hiragino Sans",sans-serif;line-height:1.7}
main{max-width:720px;margin:0 auto;padding:16px}
header{background:#1f4e5f;color:#fff;padding:16px}
header a{color:#fff;text-decoration:none;font-weight:700}
h1{font-size:1.3rem}
.post-container{background:#fff;border:1px solid #e3e1da;border-radius:8px;padding:8px 14px;margin:10px 0}
.id-line{margin:0;color:#667;font-size:.8rem}
.user-name{color:#1a7f4b;font-weight:600}
.post-content{margin:4px 0 0;word-break:break-word}
.article-list{list-style:none;padding:0}
.article-list li{background:#fff;border:1px solid #e3e1da;border-radius:8px;margin:8px 0;padding:10px 14px}
.article-list time{display:block;color:#667;font-size:.8rem}
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted_threads (
            filename TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            title TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)


class StaticSitePublisher:
    def __init__(self, mode: str, site_title: str, base_url: str, output_root: str, db_path: str):
        self.mode = mode
        self.site_title = site_title
        self.base_url = base_url.rstrip("/")
        self.output_dir = os.path.join(output_root, mode)
        self.db_path = db_path
        with sqlite3.connect(db_path) as conn:
            ensure_table(conn)

    # --- ページの組み立て ---------------------------------------------------

    def _page(self, title: str, body: str, prefix: str = "") -> str:
        return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="{prefix}style.css">
<link rel="alternate" type="application/rss+xml" href="{prefix}feed.xml">
</head>
<body>
<header><a href="{prefix}index.html">{escape(self.site_title)}</a></header>
<main>{body}</main>
</body>
</html>
"""

    def render_thread(self, title: str, thread_html: str, prefix: str = "../") -> str:
        """記事ページの HTML を返す。`thread_html` はエスケープ済み。"""
        return self._page(f"{title} | {self.site_title}",
                          f"<h1>{escape(title)}</h1>\n{thread_html}", prefix)

    # --- 公開・取り下げ -----------------------------------------------------

    def publish_thread(self, title: str, thread_html: str, raw_text: str, *, topic: str = "",
                       filename: str = "") -> str:
        """記事を書き出して保存先パスを返す。

        filename を渡すと同じ URL で公開する（取り下げた記事の再公開用）。
        """
        if not filename:
            seed = f"{self.mode}\n{title}\n{time.time_ns()}".encode("utf-8")
            filename = hashlib.sha256(seed).hexdigest()[:12] + ".html"
        path = os.path.join(self.output_dir, "threads", filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.render_thread(title, thread_html))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO posted_threads"
                " (filename, mode, title, raw_text, topic, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (filename, self.mode, title, raw_text, topic, now_utc()),
            )
        self.generate_site_files()
        return path

    def get_thread(self, filename: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM posted_threads WHERE filename = ? AND mode = ?",
                (filename, self.mode)).fetchone()
        return dict(row) if row else None

    def remove_thread(self, filename: str) -> bool:
        """公開中の記事を消す。ファイルの場所は DB の行で確かめてから触る。"""
        if self.get_thread(filename) is None:
            return False
        path = os.path.join(self.output_dir, "threads", os.path.basename(filename))
        if os.path.exists(path):
            os.remove(path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM posted_threads WHERE filename = ? AND mode = ?",
                         (filename, self.mode))
        self.generate_site_files()
        return True

    def list_threads(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM posted_threads WHERE mode = ? ORDER BY created_at DESC, rowid DESC",
                (self.mode,)).fetchall()
        return [dict(r) for r in rows]

    # --- 一覧・RSS・サイトマップ --------------------------------------------

    def generate_site_files(self) -> None:
        threads = self.list_threads()
        os.makedirs(self.output_dir, exist_ok=True)
        rows = "\n".join(
            f'<li><a href="threads/{escape(t["filename"])}">{escape(t["title"])}</a>'
            f'<time>{utc_to_jst(t["created_at"])}</time></li>' for t in threads)
        files = {
            "style.css": STYLE,
            "index.html": self._page(self.site_title, f'<ul class="article-list">{rows}</ul>'),
            "feed.xml": self._feed(threads[:20]),
            "sitemap.xml": self._sitemap(threads),
        }
        for name, text in files.items():
            with open(os.path.join(self.output_dir, name), "w", encoding="utf-8") as f:
                f.write(text)

    def _url(self, thread: dict) -> str:
        return f"{self.base_url}/threads/{thread['filename']}"

    def _feed(self, threads: list[dict]) -> str:
        items = "".join(
            f"<item><title>{xml_escape(t['title'])}</title>"
            f"<link>{xml_escape(self._url(t))}</link></item>" for t in threads)
        return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
                f"<title>{xml_escape(self.site_title)}</title>"
                f"<link>{xml_escape(self.base_url)}/</link>{items}</channel></rss>\n")

    def _sitemap(self, threads: list[dict]) -> str:
        urls = "".join(f"<url><loc>{xml_escape(self._url(t))}</loc></url>" for t in threads)
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n')
