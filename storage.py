"""SQLite (local) / Postgres (Railway) storage layer.

Selects the backend at import time based on DATABASE_URL. The two backends
share an identical schema and an identical public API; query syntax is
adapted via the BACKEND-aware query builders.
"""
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from config import DB_PATH, DATABASE_URL, MISSED_CUTOFF_HOUR, TZ, USE_POSTGRES

log = logging.getLogger("med_bot.storage")


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_log (
    date            TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'taken', 'missed')),
    taken_at        TEXT,
    sticker_index   INTEGER,
    reminder_msg_id TEXT
)
"""


# --- backend setup --------------------------------------------------------

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

    PARAM = "%s"

    @contextmanager
    def _conn():
        con = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _row_get(row, key):
        return row[key] if row else None

    INSERT_IGNORE_DAY = (
        f"INSERT INTO daily_log (date, status) VALUES ({PARAM}, 'pending') "
        "ON CONFLICT (date) DO NOTHING"
    )
    UPSERT_TAKEN = (
        f"INSERT INTO daily_log (date, status, taken_at, sticker_index) "
        f"VALUES ({PARAM}, 'taken', {PARAM}, {PARAM}) "
        "ON CONFLICT (date) DO UPDATE SET "
        "status = 'taken', taken_at = EXCLUDED.taken_at, sticker_index = EXCLUDED.sticker_index"
    )
    CUSTOM_STICKERS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS custom_stickers (
        filename    TEXT PRIMARY KEY,
        image       BYTEA NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """
    UPSERT_STICKER = (
        f"INSERT INTO custom_stickers (filename, image) VALUES ({PARAM}, {PARAM}) "
        "ON CONFLICT (filename) DO UPDATE SET image = EXCLUDED.image"
    )
    SUPABASE_GRANTS_TABLES = ("daily_log", "custom_stickers")
    # Per-table grants run in their own transaction so a failure on one table
    # (or on the GRANTS step itself) cannot roll back the preceding CREATE
    # TABLE. The service_role GRANT is required for new public tables to be
    # visible via the Supabase Data API / dashboard under the May-30 default
    # (new tables get no role grants without an explicit GRANT).
    SUPABASE_GRANTS_PER_TABLE = """
DO $$
BEGIN
  ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON public.{table} FROM anon;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON public.{table} FROM authenticated;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO service_role;
  END IF;
END $$;
"""
else:
    import sqlite3

    PARAM = "?"

    @contextmanager
    def _conn():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _row_get(row, key):
        return row[key] if row is not None else None

    INSERT_IGNORE_DAY = (
        f"INSERT OR IGNORE INTO daily_log (date, status) VALUES ({PARAM}, 'pending')"
    )
    UPSERT_TAKEN = (
        f"INSERT INTO daily_log (date, status, taken_at, sticker_index) "
        f"VALUES ({PARAM}, 'taken', {PARAM}, {PARAM}) "
        "ON CONFLICT(date) DO UPDATE SET "
        "status = 'taken', taken_at = excluded.taken_at, sticker_index = excluded.sticker_index"
    )
    CUSTOM_STICKERS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS custom_stickers (
        filename    TEXT PRIMARY KEY,
        image       BLOB NOT NULL,
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """
    UPSERT_STICKER = (
        f"INSERT INTO custom_stickers (filename, image) VALUES ({PARAM}, {PARAM}) "
        "ON CONFLICT(filename) DO UPDATE SET image = excluded.image"
    )


# --- public API -----------------------------------------------------------

def init_db() -> None:
    log.info("storage backend: %s", "postgres" if USE_POSTGRES else "sqlite")
    with _conn() as c:
        c.execute(SCHEMA)
        c.execute(CUSTOM_STICKERS_SCHEMA)
    if USE_POSTGRES:
        for table in SUPABASE_GRANTS_TABLES:
            try:
                with _conn() as c:
                    c.execute(SUPABASE_GRANTS_PER_TABLE.format(table=table))
            except Exception as e:
                log.warning(
                    "supabase RLS/grants step failed for %s "
                    "(table itself is created): %s",
                    table,
                    e,
                )


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


def medication_day_str() -> str:
    """The active medication day. Until MISSED_CUTOFF_HOUR (02:00 local), the
    previous calendar day still owns any pending reminder, so 'yes' or a ✅
    reaction at 01:30 Sunday should resolve Saturday's reminder."""
    now = datetime.now(TZ)
    d = now.date()
    if now.hour < MISSED_CUTOFF_HOUR:
        d -= timedelta(days=1)
    return d.isoformat()


def ensure_day(d: str) -> None:
    with _conn() as c:
        c.execute(INSERT_IGNORE_DAY, (d,))


def set_reminder_msg(d: str, msg_id: int) -> None:
    with _conn() as c:
        c.execute(
            f"UPDATE daily_log SET reminder_msg_id = {PARAM} WHERE date = {PARAM}",
            (str(msg_id), d),
        )


def get_status(d: str):
    with _conn() as c:
        cur = c.execute(
            f"SELECT * FROM daily_log WHERE date = {PARAM}", (d,)
        )
        return cur.fetchone()


def get_reminder_msg_id(d: str) -> Optional[int]:
    row = get_status(d)
    if row is None:
        return None
    val = _row_get(row, "reminder_msg_id")
    return int(val) if val else None


def mark_taken(d: str, sticker_index: int) -> bool:
    """Returns True if newly marked, False if already taken or missed.

    A 'missed' day cannot be retroactively marked taken — once the 02:00
    cutoff has run for that day, the entry is final.
    """
    with _conn() as c:
        cur = c.execute(
            f"SELECT status FROM daily_log WHERE date = {PARAM}", (d,)
        )
        row = cur.fetchone()
        if row and _row_get(row, "status") in ("taken", "missed"):
            return False
        now_iso = datetime.now(TZ).isoformat(timespec="seconds")
        c.execute(UPSERT_TAKEN, (d, now_iso, sticker_index))
        return True


def mark_missed_if_pending(d: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            f"UPDATE daily_log SET status = 'missed' "
            f"WHERE date = {PARAM} AND status = 'pending'",
            (d,),
        )
        return cur.rowcount > 0


def is_pending(d: str) -> bool:
    row = get_status(d)
    return row is not None and _row_get(row, "status") == "pending"


def range_logs(start: date, end: date) -> list:
    with _conn() as c:
        cur = c.execute(
            f"SELECT * FROM daily_log WHERE date >= {PARAM} AND date <= {PARAM} "
            "ORDER BY date",
            (start.isoformat(), end.isoformat()),
        )
        return list(cur.fetchall())


def status_map(start: date, end: date) -> dict:
    return {_row_get(r, "date"): r for r in range_logs(start, end)}


def current_streak() -> int:
    """Consecutive 'taken' days ending yesterday or today."""
    today = datetime.now(TZ).date()
    logs = status_map(today - timedelta(days=365), today)
    cursor = today
    if _row_get(logs.get(today.isoformat()), "status") != "taken":
        cursor = today - timedelta(days=1)
    streak = 0
    while True:
        row = logs.get(cursor.isoformat())
        if row and _row_get(row, "status") == "taken":
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break
    return streak


def counts(start: date, end: date) -> dict:
    rows = range_logs(start, end)
    out = {"taken": 0, "missed": 0, "pending": 0}
    for r in rows:
        s = _row_get(r, "status")
        out[s] = out.get(s, 0) + 1
    return out


def dump_all_rows() -> list[dict]:
    """Return every daily_log row as a plain list of dicts. Used for backup DMs."""
    with _conn() as c:
        cur = c.execute("SELECT * FROM daily_log ORDER BY date")
        rows = cur.fetchall()
    fields = ("date", "status", "taken_at", "sticker_index", "reminder_msg_id")
    return [{f: _row_get(r, f) for f in fields} for r in rows]


# --- custom sticker blob storage ------------------------------------------

def save_sticker_blob(filename: str, data: bytes) -> None:
    with _conn() as c:
        c.execute(UPSERT_STICKER, (filename, data))


def delete_sticker_blob(filename: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            f"DELETE FROM custom_stickers WHERE filename = {PARAM}", (filename,)
        )
        return cur.rowcount > 0


def list_sticker_blobs() -> list[tuple[str, bytes]]:
    with _conn() as c:
        cur = c.execute(
            "SELECT filename, image FROM custom_stickers ORDER BY filename"
        )
        rows = cur.fetchall()
    return [(_row_get(r, "filename"), bytes(_row_get(r, "image"))) for r in rows]


def get_taken_builtin_dates(builtin_count: int) -> list[str]:
    """Return dates of all 'taken' rows with a built-in sticker index (< builtin_count)."""
    with _conn() as c:
        cur = c.execute(
            f"SELECT date FROM daily_log "
            f"WHERE status = 'taken' AND (sticker_index IS NULL OR sticker_index < {PARAM})",
            (builtin_count,),
        )
        rows = cur.fetchall()
    return [_row_get(r, "date") for r in rows]


def update_sticker_index(d: str, sticker_index: int) -> None:
    with _conn() as c:
        c.execute(
            f"UPDATE daily_log SET sticker_index = {PARAM} WHERE date = {PARAM}",
            (sticker_index, d),
        )
