"""生成の再試行。

1日1回まとめて生成し、時間差で1本ずつ公開するモードでは、生成の1回の失敗が
その日の記事ゼロに直結する。実際に2日続けて Gemini が `503 UNAVAILABLE`
（混雑）を返し、プライマリ・セカンダリとも3回ずつ失敗して公開が止まった。

**失敗した日だけ、10分おきに生成し直す。** cron には `--retry` 付きの行を
10分おきに並べておき、実行するかは `should_retry()` がその場で決める。

- 生成の時刻をずらす案は採らなかった。503 は全体の混雑で、時刻を選べば
  避けられるものではない。
- **再試行では、今日このあと公開する回数（`remaining_slots()`）だけ作る。**
  枠より多く作ると余りが翌日に持ち越され、fifo なので翌日は前日の古い
  話題から出てしまう。残りが0なら作らない。
- 締め切り（`generate_retry_until`）は最後の公開の前に置く。

cron から軽く呼べるよう、**標準ライブラリだけで書く。**
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

RETRY_INTERVAL_MIN = 10

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_until(value: str) -> tuple[int, int] | None:
    """`"18:00"` → `(18, 0)`。空・不正なら None（再試行しない）。"""
    m = _HHMM.match((value or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def retry_crons(schedule: str, until: str) -> list[str]:
    """生成の cron 式と締め切りから、再試行の cron 式を作る。

    生成の10分後から締め切りまで10分おき。**分と時が1つずつの cron 式にだけ
    対応する**（`0 16 * * 1-5` など）。1日に何度も生成するモードは、次の回が
    再試行を兼ねるので要らない。対応しない形なら空を返す。
    """
    end = parse_until(until)
    parts = (schedule or "").split()
    if not end or len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        return []
    start = int(parts[1]) * 60 + int(parts[0])
    stop = end[0] * 60 + end[1]
    rest = " ".join(parts[2:])
    by_hour: dict[int, list[int]] = {}
    t = start + RETRY_INTERVAL_MIN
    while t <= stop and t < 24 * 60:
        by_hour.setdefault(t // 60, []).append(t % 60)
        t += RETRY_INTERVAL_MIN
    return [f"{','.join(map(str, mins))} {h} {rest}" for h, mins in sorted(by_hour.items())]


def queued_drafts(db_path: str, site: str) -> int:
    """公開待ちの下書きの数。テーブルが無ければ 0。"""
    try:
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_threads WHERE mode = ? AND state = 'queued'",
                (site,)).fetchone()[0]
    except sqlite3.Error:
        return 0


def should_retry(status: dict | None, queued: int, today: date = None) -> tuple[bool, str]:
    """今回の再試行で生成を回すか。`(回すか, 理由)`。

    再試行自身も同じ状態を書くので、失敗が続けば次の回も回り、成功すれば
    それ以降は止まる。
    """
    today = today or date.today()
    if not status:
        return False, "今日の生成がまだ走っていない"
    if (status.get("last_run_start") or "")[:10] != today.isoformat():
        return False, "今日の生成がまだ走っていない"
    state = status.get("status")
    if state == "running":
        return False, "前の生成がまだ動いている"
    if state != "error":
        return False, "今日の生成は済んでいる"
    if queued > 0:
        # 途中まで作れて落ちた。作り直すと公開の枠より多くなり、翌日へ持ち越す
        return False, f"下書きが{queued}件ある（途中まで作れている）"
    return True, "今日の生成が失敗し、下書きが無い"


def _cron_times_today(line: str, now: datetime) -> list[tuple[int, int]] | None:
    """cron 式1行のうち、今日（`now` の日）に当たる時刻。読めない形なら None。

    分は数字1つ、時は数字の並び、日・月は `*`、曜日は `*`・数字・範囲・並びだけ読む。
    """
    parts = line.split()
    if len(parts) != 5 or not parts[0].isdigit() or parts[2:4] != ["*", "*"]:
        return None
    hours = parts[1].split(",")
    if not all(h.isdigit() for h in hours):
        return None
    dow = parts[4]
    if dow != "*":
        days = set()
        for piece in dow.split(","):
            a, _, b = piece.partition("-")
            if not a.isdigit() or (b and not b.isdigit()):
                return None
            days.update(range(int(a), int(b or a) + 1))
        if 7 in days:
            days.add(0)
        if (now.weekday() + 1) % 7 not in days:   # cron は日曜=0、Python は月曜=0
            return []
    return [(int(h), int(parts[0])) for h in hours]


def remaining_slots(publish_schedule: str, now: datetime) -> int | None:
    """今日このあと公開する回数。数えられなければ None。"""
    count = 0
    for line in (l.strip() for l in (publish_schedule or "").splitlines()):
        if not line:
            continue
        times = _cron_times_today(line, now)
        if times is None:
            return None
        count += sum(1 for h, m in times if (h, m) > (now.hour, now.minute))
    return count
