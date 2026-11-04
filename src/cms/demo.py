"""デモ用の架空データ。API キーなしで、本物のパイプラインを一通り通す。

**差し替えるのは AI の応答だけ。** 検証・キュー・公開・ログは本番と同じ経路を通る。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cms import article_queue, config, pipeline, run_log
from cms.models import Article
from cms.publisher import StaticSitePublisher
from cms.repository import SQLiteArticleRepository

DEMO_ARTICLES = [
    Article("架空企業Acme、開発者向けの新しい言語を発表", "処理系を軽量化した新言語を公開した。",
            "https://example.com/news/1"),
    Article("静的サイトの配信速度が3割改善、エッジ配信の最新事情", "配信拠点を増やす手法が広がっている。",
            "https://example.com/news/2"),
    Article("週末に試したいオープンソースのデータベース5選", "軽量な組み込み型が人気を集めている。",
            "https://example.com/news/3"),
    Article("リモート開発環境は定着するか、現場の声を集めた", "ローカルとの使い分けが進んでいる。",
            "https://example.com/news/4"),
]

REPLIES = (
    "{topic}について語りましょう",
    "前から気になっていました",
    ">>1\n詳しい人はいますか",
    "調べてみたら思ったより奥が深いです",
    ">>3\n私も知りたいです",
    "まず一次情報を読むのが早いと思います",
)


class DemoGenerator:
    """AI の代わりに決まった形の応答を返す。`script` で失敗も再現できる。

    script の要素: "ok" / "title_only"（検証で弾かれる応答）/ "busy"（503）
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = 0
        self.last_used_account = None
        self.last_usage = None

    def generate(self, prompt: str) -> str:
        step = self.script[self.calls] if self.calls < len(self.script) else "ok"
        self.calls += 1
        if step == "busy":
            raise RuntimeError("503 UNAVAILABLE: The model is overloaded.")
        self.last_used_account = "secondary" if self.calls % 4 == 0 else "primary"
        self.last_usage = {"prompt": 900, "output": 2100 + self.calls * 37,
                           "total": 3000 + self.calls * 37}
        m = re.search(r"題材[:：]\s*(.+)", prompt)
        topic = m.group(1).strip() if m else "今日の話題"
        title = f"【話題】{topic}"
        if step == "title_only":
            return title
        return title + "".join(f"\n^\n{r.format(topic=topic)}" for r in REPLIES)


def seed(root: str, db_path: str, output_root: str) -> None:
    """デモの状態を作り直す。本番用の DB と output/ には触らない（呼び出し側が
    デモ用のパスを渡す）。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(db_path).unlink(missing_ok=True)

    modes = config.load_modes(root)
    for name, mode in modes.items():
        shutil.rmtree(Path(output_root, name), ignore_errors=True)
        publisher = StaticSitePublisher(name, mode["site_title"], mode["base_url"],
                                        output_root, db_path)
        prompt = config.load_prompt(mode, root)
        if mode["type"] == "rss":
            # 2本目は検証で弾かれる応答、3本目は Gemini の 503。失敗しても次へ進む
            pipeline.generate_from_articles(
                DemoGenerator(["ok", "title_only", "busy", "ok"]), DEMO_ARTICLES, prompt,
                SQLiteArticleRepository(db_path), db_path, name, mode, count=2)
            pipeline.publish_next(publisher, mode)
        else:
            from cms.items_loader import load_items
            pipeline.generate_from_items(DemoGenerator(), load_items(mode, root), prompt,
                                         db_path, name, mode, count=6)
            _arrange_queue(publisher, mode)
    run_log.record(db_path, "history-trivia", "skipped", error="下書きがありません")


def _arrange_queue(publisher, mode: dict) -> None:
    """公開・予約・取り下げ・ゴミ箱の各状態が画面に出るように動かす。"""
    for _ in range(3):
        pipeline.publish_next(publisher, mode)
    pipeline.unpublish(publisher, publisher.list_threads()[-1]["filename"])
    with sqlite3.connect(publisher.db_path) as conn:
        queued = article_queue.list_items(conn, publisher.mode, "queued")
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        article_queue.schedule(conn, queued[0]["id"], tomorrow.strftime("%Y-%m-%d %H:%M:%S"))
        article_queue.trash(conn, queued[-1]["id"])

