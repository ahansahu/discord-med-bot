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
    TODO_SECTIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS todo_sections (
        id        BIGSERIAL PRIMARY KEY,
        name      TEXT NOT NULL UNIQUE,
        position  INTEGER NOT NULL
    )
    """
    TODO_ITEMS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS todo_items (
        id         BIGSERIAL PRIMARY KEY,
        section_id BIGINT NOT NULL REFERENCES todo_sections(id) ON DELETE CASCADE,
        text       TEXT NOT NULL,
        position   INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """
    TODO_STATE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS todo_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """
    INSERT_SECTION_RETURN_ID = (
        f"INSERT INTO todo_sections (name, position) VALUES ({PARAM}, {PARAM}) RETURNING id"
    )
    INSERT_ITEM_RETURN_ID = (
        f"INSERT INTO todo_items (section_id, text, position, created_at) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}, {PARAM}) RETURNING id"
    )
    UPSERT_TODO_STATE = (
        f"INSERT INTO todo_state (key, value) VALUES ({PARAM}, {PARAM}) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    SUPABASE_GRANTS_TABLES = (
        "daily_log",
        "custom_stickers",
        "todo_sections",
        "todo_items",
        "todo_state",
    )
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
    TODO_SECTIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS todo_sections (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL UNIQUE,
        position  INTEGER NOT NULL
    )
    """
    TODO_ITEMS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS todo_items (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        section_id INTEGER NOT NULL REFERENCES todo_sections(id) ON DELETE CASCADE,
        text       TEXT NOT NULL,
        position   INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """
    TODO_STATE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS todo_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """
    INSERT_SECTION_RETURN_ID = (
        f"INSERT INTO todo_sections (name, position) VALUES ({PARAM}, {PARAM})"
    )
    INSERT_ITEM_RETURN_ID = (
        f"INSERT INTO todo_items (section_id, text, position, created_at) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}, {PARAM})"
    )
    UPSERT_TODO_STATE = (
        f"INSERT INTO todo_state (key, value) VALUES ({PARAM}, {PARAM}) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )


# --- public API -----------------------------------------------------------

def init_db() -> None:
    log.info("storage backend: %s", "postgres" if USE_POSTGRES else "sqlite")
    with _conn() as c:
        c.execute(SCHEMA)
        c.execute(CUSTOM_STICKERS_SCHEMA)
        c.execute(TODO_SECTIONS_SCHEMA)
        c.execute(TODO_ITEMS_SCHEMA)
        c.execute(TODO_STATE_SCHEMA)
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


# --- todo list -----------------------------------------------------------

def _row_to_section(r) -> dict:
    return {
        "id": _row_get(r, "id"),
        "name": _row_get(r, "name"),
        "position": _row_get(r, "position"),
    }


def _row_to_item(r) -> dict:
    return {
        "id": _row_get(r, "id"),
        "section_id": _row_get(r, "section_id"),
        "text": _row_get(r, "text"),
        "position": _row_get(r, "position"),
        "created_at": _row_get(r, "created_at"),
    }


def todo_sections_ordered() -> list[dict]:
    with _conn() as c:
        cur = c.execute(
            "SELECT id, name, position FROM todo_sections ORDER BY position, id"
        )
        return [_row_to_section(r) for r in cur.fetchall()]


def todo_section_by_name(name: str) -> Optional[dict]:
    with _conn() as c:
        cur = c.execute(
            f"SELECT id, name, position FROM todo_sections WHERE name = {PARAM}",
            (name,),
        )
        row = cur.fetchone()
    return _row_to_section(row) if row else None


def todo_section_create(name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("section name cannot be empty")
    with _conn() as c:
        cur = c.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM todo_sections")
        pos = _row_get(cur.fetchone(), "p") or 0
        if USE_POSTGRES:
            cur = c.execute(INSERT_SECTION_RETURN_ID, (name, pos))
            return _row_get(cur.fetchone(), "id")
        cur = c.execute(INSERT_SECTION_RETURN_ID, (name, pos))
        return cur.lastrowid


def todo_section_rename(section_id: int, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("section name cannot be empty")
    with _conn() as c:
        c.execute(
            f"UPDATE todo_sections SET name = {PARAM} WHERE id = {PARAM}",
            (new_name, section_id),
        )


def todo_section_delete(section_id: int) -> int:
    """Delete a section and its items. Returns count of items removed."""
    with _conn() as c:
        cur = c.execute(
            f"DELETE FROM todo_items WHERE section_id = {PARAM}", (section_id,)
        )
        removed = cur.rowcount or 0
        c.execute(f"DELETE FROM todo_sections WHERE id = {PARAM}", (section_id,))
    return removed


def todo_section_move(section_id: int, direction: str) -> bool:
    sections = todo_sections_ordered()
    idx = next((i for i, s in enumerate(sections) if s["id"] == section_id), -1)
    if idx < 0:
        return False
    if direction == "up" and idx == 0:
        return False
    if direction == "down" and idx == len(sections) - 1:
        return False
    other_idx = idx - 1 if direction == "up" else idx + 1
    a, b = sections[idx], sections[other_idx]
    with _conn() as c:
        c.execute(
            f"UPDATE todo_sections SET position = {PARAM} WHERE id = {PARAM}",
            (b["position"], a["id"]),
        )
        c.execute(
            f"UPDATE todo_sections SET position = {PARAM} WHERE id = {PARAM}",
            (a["position"], b["id"]),
        )
    return True


def todo_items_for_section(section_id: int) -> list[dict]:
    with _conn() as c:
        cur = c.execute(
            f"SELECT id, section_id, text, position, created_at FROM todo_items "
            f"WHERE section_id = {PARAM} ORDER BY position, id",
            (section_id,),
        )
        return [_row_to_item(r) for r in cur.fetchall()]


def todo_items_all_ordered() -> list[dict]:
    """All items in section-then-position order. Each row gets `section_name`."""
    with _conn() as c:
        cur = c.execute(
            "SELECT i.id, i.section_id, i.text, i.position, i.created_at, "
            "s.name AS section_name, s.position AS section_position "
            "FROM todo_items i JOIN todo_sections s ON s.id = i.section_id "
            "ORDER BY s.position, s.id, i.position, i.id"
        )
        out = []
        for r in cur.fetchall():
            d = _row_to_item(r)
            d["section_name"] = _row_get(r, "section_name")
            out.append(d)
        return out


def todo_item_get(item_id: int) -> Optional[dict]:
    with _conn() as c:
        cur = c.execute(
            f"SELECT id, section_id, text, position, created_at FROM todo_items "
            f"WHERE id = {PARAM}",
            (item_id,),
        )
        row = cur.fetchone()
    return _row_to_item(row) if row else None


def todo_item_add(section_id: int, text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("item text cannot be empty")
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            f"SELECT COALESCE(MAX(position), -1) + 1 AS p FROM todo_items "
            f"WHERE section_id = {PARAM}",
            (section_id,),
        )
        pos = _row_get(cur.fetchone(), "p") or 0
        if USE_POSTGRES:
            cur = c.execute(INSERT_ITEM_RETURN_ID, (section_id, text, pos, now))
            return _row_get(cur.fetchone(), "id")
        cur = c.execute(INSERT_ITEM_RETURN_ID, (section_id, text, pos, now))
        return cur.lastrowid


def todo_item_delete(item_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            f"DELETE FROM todo_items WHERE id = {PARAM}", (item_id,)
        )
        return (cur.rowcount or 0) > 0


def todo_item_move(item_id: int, direction: str) -> bool:
    item = todo_item_get(item_id)
    if item is None:
        return False
    siblings = todo_items_for_section(item["section_id"])
    idx = next((i for i, x in enumerate(siblings) if x["id"] == item_id), -1)
    if idx < 0:
        return False
    if direction == "up" and idx == 0:
        return False
    if direction == "down" and idx == len(siblings) - 1:
        return False
    other_idx = idx - 1 if direction == "up" else idx + 1
    a, b = siblings[idx], siblings[other_idx]
    with _conn() as c:
        c.execute(
            f"UPDATE todo_items SET position = {PARAM} WHERE id = {PARAM}",
            (b["position"], a["id"]),
        )
        c.execute(
            f"UPDATE todo_items SET position = {PARAM} WHERE id = {PARAM}",
            (a["position"], b["id"]),
        )
    return True


def todo_item_priority(item_id: int, level: str) -> bool:
    """level='high' → jump to top of section; level='low' → jump to bottom."""
    item = todo_item_get(item_id)
    if item is None:
        return False
    siblings = todo_items_for_section(item["section_id"])
    if not siblings:
        return False
    if level == "high":
        new_pos = siblings[0]["position"] - 1
    elif level == "low":
        new_pos = siblings[-1]["position"] + 1
    else:
        return False
    with _conn() as c:
        c.execute(
            f"UPDATE todo_items SET position = {PARAM} WHERE id = {PARAM}",
            (new_pos, item_id),
        )
    return True


def todo_get_state(key: str) -> Optional[str]:
    with _conn() as c:
        cur = c.execute(
            f"SELECT value FROM todo_state WHERE key = {PARAM}", (key,)
        )
        row = cur.fetchone()
    return _row_get(row, "value") if row else None


def todo_set_state(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(UPSERT_TODO_STATE, (key, value))
