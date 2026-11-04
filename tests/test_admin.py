"""管理UIの3画面と、デモデータの通し確認。"""
import sqlite3

import pytest

from cms import article_queue as q
from cms import config, demo, run_log
from cms.admin.app import create_app


@pytest.fixture
def env(tmp_path):
    db, out = str(tmp_path / "demo.db"), str(tmp_path / "out")
    demo.seed(config.PROJECT_ROOT, db, out)
    client = create_app(config.PROJECT_ROOT, db, out, demo=True).test_client()
    return client, db


def items(db, state=None):
    with sqlite3.connect(db) as conn:
        return q.list_items(conn, state=state)


@pytest.mark.parametrize("path", ["/queue", "/queue/trash", "/logs", "/schedule",
                                  "/site/tech-news/", "/site/history-trivia/feed.xml"])
def test_pages_open(env, path):
    assert env[0].get(path).status_code == 200


def test_demo_shows_every_state(env):
    _, db = env
    states = {i["state"] for i in items(db)}
    assert states == set(q.STATES)
    with sqlite3.connect(db) as conn:
        assert q.list_trashed(conn)
    assert {r["status"] for r in run_log.list_logs(db)} == set(run_log.STATUSES)


def test_demo_records_generation_failures(env):
    errors = run_log.list_logs(env[1], status="error")
    assert any("503" in r["error"] for r in errors)
    assert any("レス" in r["error"] for r in errors)


def test_publish_button(env):
    client, db = env
    item = items(db, "queued")[0]
    res = client.post(f"/queue/{item['id']}/publish")
    assert res.status_code == 302
    assert all(i["id"] != item["id"] for i in items(db))


def test_schedule_converts_jst_to_utc(env):
    client, db = env
    item = items(db, "queued")[0]
    client.post(f"/queue/{item['id']}/schedule", data={"publish_at": "2030-01-01T09:00"})
    with sqlite3.connect(db) as conn:
        assert q.get(conn, item["id"])["publish_at"] == "2030-01-01 00:00:00"


def test_invalid_schedule_shows_error(env):
    client, db = env
    item = items(db, "queued")[0]
    res = client.post(f"/queue/{item['id']}/schedule", data={"publish_at": "x"},
                      follow_redirects=True)
    assert "形式が正しくありません" in res.get_data(as_text=True)


def test_delete_and_restore(env):
    client, db = env
    item = items(db, "queued")[0]
    client.post(f"/queue/{item['id']}/delete")
    assert all(i["id"] != item["id"] for i in items(db))
    client.post(f"/queue/trash/{item['id']}/restore")
    assert any(i["id"] == item["id"] for i in items(db))


def test_unpublish_from_logs(env):
    client, db = env
    row = next(r for r in run_log.list_logs(db, "tech-news", "success"))
    client.post("/logs/unpublish", data={"mode": "tech-news", "filename": row["filename"]})
    assert items(db, "hidden")
    assert client.get(f"/site/tech-news/threads/{row['filename']}").status_code == 404


def test_preview_does_not_publish(env):
    client, db = env
    item = items(db, "queued")[0]
    before = len(items(db))
    res = client.get(f"/queue/{item['id']}/preview")
    assert res.status_code == 200 and "post-container" in res.get_data(as_text=True)
    assert len(items(db)) == before


def test_site_rejects_unknown_mode_and_traversal(env):
    client, _ = env
    assert client.get("/site/nope/").status_code == 404
    assert client.get("/site/tech-news/../../demo.db").status_code == 404


def test_titles_are_escaped(env):
    client, db = env
    with sqlite3.connect(db) as conn:
        q.add(conn, "tech-news", "<script>x</script>\n^\na\n^\nb\n^\nc")
    assert "<script>x" not in client.get("/queue").get_data(as_text=True)
