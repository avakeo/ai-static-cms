"""`cms generate --retry` の判定と、状態の記録。"""
import pytest

from cms import cli, config
from cms.demo import DemoGenerator


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "items").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "modes").mkdir()
    (tmp_path / "items/i.txt").write_text("甲\n乙\n丙\n", encoding="utf-8")
    (tmp_path / "prompts/p.txt").write_text("題材: {ITEM}", encoding="utf-8")
    (tmp_path / "modes/m.toml").write_text(
        'site_title="s"\nbase_url="https://e"\ntype="list"\nprompt_file="prompts/p.txt"\n'
        'items_file="items/i.txt"\nschedule="0 7 * * *"\npublish_schedule="0 23 * * *"\n'
        'batch_count=3\n', encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
    return tmp_path


def run(argv):
    return cli.main(argv)


def test_generate_records_success(root, monkeypatch):
    monkeypatch.setattr(cli, "build_generator", lambda: DemoGenerator())
    assert run(["generate", "m"]) == 0
    assert cli.load_status(str(root))["m"]["status"] == "success"


def test_total_failure_records_error_and_retry_recovers(root, monkeypatch):
    monkeypatch.setattr(cli, "build_generator", lambda: DemoGenerator(["busy"] * 9))
    assert run(["generate", "m"]) == 1
    assert cli.load_status(str(root))["m"]["status"] == "error"

    monkeypatch.setattr(cli, "build_generator", lambda: DemoGenerator())
    monkeypatch.setattr(cli.generate_retry, "remaining_slots", lambda *a: 2)
    assert run(["generate", "m", "--retry"]) == 0
    assert cli.generate_retry.queued_drafts(str(root / "data/cms.db"), "m") == 2


def test_retry_does_nothing_after_success(root, monkeypatch):
    monkeypatch.setattr(cli, "build_generator", lambda: DemoGenerator())
    run(["generate", "m"])
    monkeypatch.setattr(cli, "build_generator", lambda: pytest.fail("生成してはいけない"))
    assert run(["generate", "m", "--retry"]) == 0


def test_publish_due_publishes_only_scheduled(root, monkeypatch):
    import sqlite3
    from cms import article_queue as q
    monkeypatch.setattr(cli, "build_generator", lambda: DemoGenerator())
    run(["generate", "m"])
    db = str(root / "data/cms.db")
    with sqlite3.connect(db) as conn:
        first = q.list_items(conn, "m")[0]["id"]
        q.schedule(conn, first, "2020-01-01 00:00:00")
    run(["publish-due"])
    assert cli.generate_retry.queued_drafts(db, "m") == 2
    assert len(list((root / "output/m/threads").glob("*.html"))) == 1
