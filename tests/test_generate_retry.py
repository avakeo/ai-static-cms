"""生成の再試行：失敗した日だけ、公開枠の分だけ作り直す。"""
import sqlite3
from datetime import date, datetime

import pytest

import cms.article_queue as q
import cms.generate_retry as gr

TODAY = date(2026, 9, 30)


def status(state, start="2026-09-30T16:00:01"):
    return {"status": state, "last_run_start": start}


def test_parse_until():
    assert gr.parse_until("18:00") == (18, 0)
    assert gr.parse_until("25:00") is None
    assert gr.parse_until("") is None


def test_retry_crons_every_ten_minutes():
    assert gr.retry_crons("0 16 * * 1-5", "17:10") == [
        "10,20,30,40,50 16 * * 1-5", "0,10 17 * * 1-5"]


@pytest.mark.parametrize("schedule,until", [
    ("0 8,12 * * *", "18:00"),  # 1日に複数回の生成は次の回が再試行を兼ねる
    ("*/5 * * * *", "18:00"),
    ("0 16 * * *", ""),
])
def test_retry_crons_unsupported(schedule, until):
    assert gr.retry_crons(schedule, until) == []


@pytest.mark.parametrize("st,queued,expected", [
    (None, 0, False),                                   # 記録なし
    (status("error", "2026-09-29T16:00:01"), 0, False), # 昨日の失敗
    (status("running"), 0, False),
    (status("success"), 0, False),
    (status("error"), 2, False),                        # 途中まで作れている
    (status("error"), 0, True),
])
def test_should_retry(st, queued, expected):
    assert gr.should_retry(st, queued, TODAY)[0] is expected


def test_remaining_slots_counts_only_future():
    assert gr.remaining_slots("0 17,18,19 * * *", datetime(2026, 9, 30, 18, 30)) == 1


def test_remaining_slots_respects_weekday():
    saturday = datetime(2026, 10, 3, 8, 0)
    assert gr.remaining_slots("0 17,18 * * 1-5", saturday) == 0


def test_remaining_slots_unreadable_cron():
    assert gr.remaining_slots("*/30 * * * *", datetime(2026, 9, 30, 8, 0)) is None


def test_queued_drafts(tmp_path):
    db = str(tmp_path / "t.db")
    assert gr.queued_drafts(db, "m") == 0
    with sqlite3.connect(db) as conn:
        q.add(conn, "m", "t")
        q.add(conn, "m", "t", state="hidden")
    assert gr.queued_drafts(db, "m") == 1
