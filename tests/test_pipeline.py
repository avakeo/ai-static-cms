"""生成 → 検証 → キュー → 公開 → 取り下げ の流れ。"""
import sqlite3

import pytest

from cms import article_queue as q
from cms import pipeline, run_log
from cms.demo import DemoGenerator
from cms.models import Article
from cms.publisher import StaticSitePublisher
from cms.repository import SQLiteArticleRepository

GOOD = "タイトル\n^\nレス1\n^\nレス2\n^\nレス3"
MODE = {"batch_count": 2, "poster_name": "名無し", "publish_order": "fifo"}


class Fixed:
    """決まった応答を順に返す。例外を入れると投げる。"""

    last_used_account = "primary"
    last_usage = {"prompt": 1, "output": 2, "total": 3}

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def article(title):
    return Article(title=title, content=f"{title}の本文", url=f"https://example.com/{title}")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def pub(tmp_path, db):
    return StaticSitePublisher("m", "サイト", "https://example.com/", str(tmp_path / "out"), db)


def queued(db):
    with sqlite3.connect(db) as conn:
        return q.list_items(conn, "m", "queued")


class TestRenderChecked:
    def test_good_response(self):
        title, html = pipeline.render_checked(GOOD, MODE)
        assert title == "タイトル"
        assert html.count("post-container") == 3

    @pytest.mark.parametrize("text", [
        "",                     # 空の応答
        "タイトルだけ",          # レスが無い
        "タイトル\n^\n\n^\n\n^\n",  # 区切りだけ
        "あ" * 200 + "\n^\nレス1\n^\nレス2\n^\nレス3",  # 本文がタイトルに混入
    ])
    def test_rejects_broken_response(self, text):
        with pytest.raises(ValueError):
            pipeline.render_checked(text, MODE)


class TestFindSimilar:
    def test_same_topic_with_different_particles(self):
        assert pipeline.find_similar("新言語を発表した", [article("新言語の発表")])

    def test_different_topic(self):
        assert not pipeline.find_similar("データベース比較", [article("新言語の発表")])


class TestGenerateFromArticles:
    def run(self, db, generator, articles):
        repo = SQLiteArticleRepository(db)
        return pipeline.generate_from_articles(
            generator, articles, "T={TITLE} C={CONTENT}", repo, db, "m", MODE), repo

    def test_stops_at_batch_count(self, db, pub):
        result, repo = self.run(db, Fixed(GOOD, GOOD), [article("赤"), article("青"), article("黄")])
        assert result == (2, 0)
        assert len(repo.get_recent_articles(10, "m")) == 2

    def test_fills_prompt(self, db, pub):
        gen = Fixed(GOOD)
        self.run(db, gen, [article("赤")])
        assert gen.prompts == ["T=赤 C=赤の本文"]

    def test_skips_similar_to_recent(self, db, pub):
        repo = SQLiteArticleRepository(db)
        repo.save_article(article("赤い本"), mode="m")
        result = pipeline.generate_from_articles(
            Fixed(GOOD), [article("赤い本")], "{TITLE}{CONTENT}", repo, db, "m", MODE)
        assert result == (0, 0)

    def test_recent_window_is_per_site(self, db, pub):
        repo = SQLiteArticleRepository(db)
        repo.save_article(article("赤い本"), mode="other")
        result = pipeline.generate_from_articles(
            Fixed(GOOD), [article("赤い本")], "{TITLE}{CONTENT}", repo, db, "m", MODE)
        assert result == (1, 0)

    def test_one_failure_does_not_stop_the_batch(self, db, pub):
        gen = Fixed(RuntimeError("503 UNAVAILABLE"), "タイトルだけ", GOOD)
        result, repo = self.run(db, gen, [article("赤"), article("青"), article("黄")])
        assert result == (1, 2)
        logs = run_log.list_logs(db, "m", "error")
        assert len(logs) == 2
        assert any("503" in r["error"] for r in logs)

    def test_failed_article_is_not_marked_used(self, db, pub):
        _, repo = self.run(db, Fixed(RuntimeError("503")), [article("赤")])
        assert repo.get_recent_articles(10, "m") == []

    def test_same_batch_is_deduplicated(self, db, pub):
        result, _ = self.run(db, Fixed(GOOD, GOOD), [article("赤い本"), article("赤い本の話")])
        assert result == (1, 0)


class TestGenerateFromItems:
    def test_skips_used_topics(self, db, pub):
        gen = Fixed(GOOD, GOOD)
        pipeline.generate_from_items(gen, ["甲"], "{ITEM}", db, "m", MODE)
        result = pipeline.generate_from_items(gen, ["甲", "乙"], "{ITEM}", db, "m", MODE)
        assert result == (1, 0)
        assert gen.prompts[-1] == "乙"

    def test_trashed_topic_can_be_reused(self, db, pub):
        pipeline.generate_from_items(Fixed(GOOD), ["甲"], "{ITEM}", db, "m", MODE)
        with sqlite3.connect(db) as conn:
            q.trash(conn, queued(db)[0]["id"])
        assert pipeline.generate_from_items(Fixed(GOOD), ["甲"], "{ITEM}", db, "m", MODE) == (1, 0)

    def test_published_topic_is_not_reused(self, db, pub):
        pipeline.generate_from_items(Fixed(GOOD), ["甲"], "{ITEM}", db, "m", MODE)
        pipeline.publish_next(pub, MODE)
        assert pipeline.generate_from_items(Fixed(GOOD), ["甲"], "{ITEM}", db, "m", MODE) == (0, 0)


class TestPublish:
    def add(self, db, text=GOOD):
        with sqlite3.connect(db) as conn:
            return q.add(conn, "m", text, topic="お題")

    def test_publishes_and_clears_queue(self, db, pub):
        self.add(db)
        path = pipeline.publish_next(pub, MODE)
        assert open(path, encoding="utf-8").read().count("post-container") == 3
        assert queued(db) == []
        assert run_log.list_logs(db, "m", "success")[0]["title"] == "タイトル"

    def test_empty_queue_is_logged_as_skipped(self, db, pub):
        assert pipeline.publish_next(pub, MODE) is None
        assert run_log.list_logs(db, "m", "skipped")

    def test_broken_draft_does_not_block_the_queue(self, db, pub):
        self.add(db, "タイトルだけ")
        self.add(db)
        assert pipeline.publish_next(pub, MODE) is None
        assert pipeline.publish_next(pub, MODE) is not None
        with sqlite3.connect(db) as conn:
            assert len(q.list_trashed(conn)) == 1

    def test_write_failure_keeps_the_draft(self, db, pub, monkeypatch):
        self.add(db)
        monkeypatch.setattr(pub, "publish_thread",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            pipeline.publish_next(pub, MODE)
        assert len(queued(db)) == 1


class TestUnpublish:
    def test_returns_to_queue_and_keeps_url(self, db, pub):
        with sqlite3.connect(db) as conn:
            q.add(conn, "m", GOOD, topic="お題")
        path = pipeline.publish_next(pub, MODE)
        filename = path.rsplit("/", 1)[-1]

        assert pipeline.unpublish(pub, filename)
        assert pub.list_threads() == []
        with sqlite3.connect(db) as conn:
            hidden = q.list_items(conn, "m", "hidden")[0]
        assert hidden["origin_filename"] == filename
        assert hidden["topic"] == "お題"

        assert pipeline.publish_item(hidden, pub, MODE) == path

    def test_unknown_file(self, pub):
        assert not pipeline.unpublish(pub, "../../etc/passwd")


def test_demo_generator_output_passes_validation():
    title, _ = pipeline.render_checked(DemoGenerator().generate("題材: テスト"), MODE)
    assert "テスト" in title
