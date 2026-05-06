import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from config import DB_PATH, TZ


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_log (
    date            TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'taken', 'missed')),
    taken_at        TEXT,
    sticker_index   INTEGER,
    reminder_msg_id TEXT
);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


def ensure_day(d: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO daily_log (date, status) VALUES (?, 'pending')",
            (d,),
        )


def set_reminder_msg(d: str, msg_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE daily_log SET reminder_msg_id = ? WHERE date = ?",
            (str(msg_id), d),
        )


def get_status(d: str) -> Optional[sqlite3.Row]:
    with _conn() as c:
        cur = c.execute("SELECT * FROM daily_log WHERE date = ?", (d,))
        return cur.fetchone()


def get_reminder_msg_id(d: str) -> Optional[int]:
    row = get_status(d)
    if row and row["reminder_msg_id"]:
        return int(row["reminder_msg_id"])
    return None


def mark_taken(d: str, sticker_index: int) -> bool:
    """Returns True if newly marked, False if already taken."""
    with _conn() as c:
        cur = c.execute("SELECT status FROM daily_log WHERE date = ?", (d,))
        row = cur.fetchone()
        if row and row["status"] == "taken":
            return False
        now_iso = datetime.now(TZ).isoformat(timespec="seconds")
        if row is None:
            c.execute(
                "INSERT INTO daily_log (date, status, taken_at, sticker_index) "
                "VALUES (?, 'taken', ?, ?)",
                (d, now_iso, sticker_index),
            )
        else:
            c.execute(
                "UPDATE daily_log SET status = 'taken', taken_at = ?, sticker_index = ? "
                "WHERE date = ?",
                (now_iso, sticker_index, d),
            )
        return True


def mark_missed_if_pending(d: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE daily_log SET status = 'missed' "
            "WHERE date = ? AND status = 'pending'",
            (d,),
        )
        return cur.rowcount > 0


def is_pending(d: str) -> bool:
    row = get_status(d)
    return row is not None and row["status"] == "pending"


def range_logs(start: date, end: date) -> list[sqlite3.Row]:
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM daily_log WHERE date >= ? AND date <= ? ORDER BY date",
            (start.isoformat(), end.isoformat()),
        )
        return list(cur.fetchall())


def status_map(start: date, end: date) -> dict[str, sqlite3.Row]:
    return {row["date"]: row for row in range_logs(start, end)}


def current_streak() -> int:
    """Consecutive 'taken' days ending yesterday or today."""
    today = datetime.now(TZ).date()
    streak = 0
    cursor = today
    today_row = get_status(today.isoformat())
    if today_row is None or today_row["status"] != "taken":
        cursor = today - timedelta(days=1)
    while True:
        row = get_status(cursor.isoformat())
        if row and row["status"] == "taken":
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break
    return streak


def counts(start: date, end: date) -> dict[str, int]:
    rows = range_logs(start, end)
    out = {"taken": 0, "missed": 0, "pending": 0}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out
