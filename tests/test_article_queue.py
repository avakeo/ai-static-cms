"""記事キューの状態機械とゴミ箱。"""
import sqlite3

import pytest

import cms.article_queue as q


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    q.ensure_table(c)
    yield c
    c.close()


def test_add_and_get(conn):
    item = q.get(conn, q.add(conn, "m", "タイトル\n^\n本文", topic="お題"))
    assert (item["state"], item["title"], item["topic"]) == ("queued", "タイトル", "お題")


def test_title_skips_leading_delimiter():
    assert q.title_of("^\nタイトル\n^\n本文") == "タイトル"
    assert q.title_of("1: ^\nタイトル") == "タイトル"


def test_title_of_blank_text_does_not_crash():
    assert q.title_of("   \n") == ""


def test_schedule_and_due(conn):
    i = q.add(conn, "m", "t")
    q.schedule(conn, i, "2026-01-01 00:00:00")
    assert q.get(conn, i)["state"] == "scheduled"
    assert q.due(conn, now="2025-12-31 23:59:59") == []
    assert [d["id"] for d in q.due(conn, now="2026-01-01 00:00:00")] == [i]


def test_clear_schedule(conn):
    i = q.add(conn, "m", "t")
    q.schedule(conn, i, "2026-01-01 00:00:00")
    q.schedule(conn, i, "")
    assert q.get(conn, i)["state"] == "queued"
    assert q.get(conn, i)["publish_at"] == ""


def test_next_item_order(conn):
    a = q.add(conn, "m", "a")
    b = q.add(conn, "m", "b")
    assert q.next_item(conn, "m", "fifo")["id"] == a
    assert q.next_item(conn, "m", "lifo")["id"] == b


def test_next_item_ignores_other_states(conn):
    q.add(conn, "m", "hidden", state="hidden")
    i = q.add(conn, "m", "scheduled")
    q.schedule(conn, i, "2026-01-01 00:00:00")
    assert q.next_item(conn, "m") is None


def test_next_item_does_not_delete(conn):
    q.add(conn, "m", "a")
    q.next_item(conn, "m")
    assert len(q.list_items(conn)) == 1


def test_counts(conn):
    q.add(conn, "m", "a")
    q.add(conn, "m", "b", state="hidden")
    assert q.counts(conn) == {"m": {"queued": 1, "hidden": 1}}


def test_trash_and_restore(conn):
    i = q.add(conn, "m", "タイトル")
    assert q.trash(conn, i)
    assert q.list_items(conn) == []
    assert q.list_trashed(conn)[0]["id"] == i
    assert q.restore(conn, i) == ""
    assert q.get(conn, i)["title"] == "タイトル"
    assert q.list_trashed(conn) == []


def test_restore_past_schedule_returns_to_queued(conn):
    # そのまま戻すと次の予約公開で即公開される
    i = q.add(conn, "m", "t")
    q.schedule(conn, i, "2020-01-01 00:00:00")
    q.trash(conn, i)
    q.restore(conn, i)
    assert q.get(conn, i)["state"] == "queued"


def test_restore_future_schedule_is_kept(conn):
    i = q.add(conn, "m", "t")
    q.schedule(conn, i, "2999-01-01 00:00:00")
    q.trash(conn, i)
    q.restore(conn, i)
    assert q.get(conn, i)["state"] == "scheduled"


def test_restore_missing(conn):
    assert q.restore(conn, 999)


def test_purge_only_touches_trash(conn):
    i = q.add(conn, "m", "t")
    assert not q.purge(conn, i)
    q.trash(conn, i)
    assert q.purge(conn, i)
    assert q.list_trashed(conn) == []


def test_jst_conversion_round_trip():
    assert q.jst_to_utc("2026-08-21T18:30") == "2026-08-21 09:30:00"
    assert q.utc_to_jst("2026-08-21 09:30:00") == "2026-08-21 18:30"


def test_jst_blank_and_invalid():
    assert q.jst_to_utc("") == ""
    with pytest.raises(ValueError):
        q.jst_to_utc("明日")
