"""元記事の保存。同じ話題を繰り返し記事にしないための履歴として使う。"""

import sqlite3
from typing import List, Protocol

from cms.models import Article


class ArticleRepository(Protocol):
    def save_article(self, article: Article, mode: str = "") -> None: ...
    def get_recent_articles(self, limit: int, mode: str | None = None) -> List[Article]: ...


class SQLiteArticleRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    url TEXT UNIQUE,
                    mode TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def save_article(self, article: Article, mode: str = "") -> None:
        """元記事を保存する。同じ URL は無視する（再取得は正常な動きなので）。

        mode: 出力先サイト単位のスコープ（`get_recent_articles` と対で使う）。
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO source_articles (title, content, url, mode)"
                " VALUES (?, ?, ?, ?)",
                (article.title, article.content, article.url, mode),
            )

    def get_recent_articles(self, limit: int, mode: str | None = None) -> List[Article]:
        """最新 N 件を返す。

        重複判定はサイト単位に絞る。モード横断で窓を共有していたころは、
        更新頻度の高いモードが他モードの窓を食い潰していた（2026-08-25）。
        """
        sql = "SELECT title, content, url FROM source_articles"
        params: tuple = ()
        if mode is not None:
            sql += " WHERE mode = ?"
            params = (mode,)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [Article(title=t, content=c, url=u) for t, c, u in rows]
