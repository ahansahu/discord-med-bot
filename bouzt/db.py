"""SQLite/Postgres backend for the Bouzt Gold feature.

All tables are prefixed `bouzt_` so the module owns its namespace and can
coexist safely with any host bot's tables in the same database.
"""
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .config import DATABASE_URL, DB_PATH, USE_POSTGRES

log = logging.getLogger("bouzt.db")


# --- backend setup --------------------------------------------------------

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

    PARAM = "%s"
    SERIAL_PK = "BIGSERIAL PRIMARY KEY"
    BIGINT = "BIGINT"

    @contextmanager
    def conn():
        c = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _row_get(row, key):
        return row[key] if row is not None else None

    INSERT_WALLET_IGNORE = (
        f"INSERT INTO bouzt_wallets (user_id, balance, created_at) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}) "
        "ON CONFLICT (user_id) DO NOTHING"
    )
    INSERT_META_UPSERT = (
        f"INSERT INTO bouzt_meta (key, value) VALUES ({PARAM}, {PARAM}) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    INSERT_COMP_RETURN_ID = (
        f"INSERT INTO bouzt_competitions (title, status, created_by, created_at, locks_at) "
        f"VALUES ({PARAM}, 'open', {PARAM}, {PARAM}, {PARAM}) RETURNING id"
    )
    INSERT_OUTCOME_RETURN_ID = (
        f"INSERT INTO bouzt_outcomes (competition_id, label, position) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}) RETURNING id"
    )
    INSERT_BET_RETURN_ID = (
        f"INSERT INTO bouzt_bets (competition_id, outcome_id, user_id, stake, placed_at) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}, {PARAM}, {PARAM}) RETURNING id"
    )
else:
    import sqlite3

    PARAM = "?"
    SERIAL_PK = "INTEGER PRIMARY KEY AUTOINCREMENT"
    BIGINT = "INTEGER"

    @contextmanager
    def conn():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _row_get(row, key):
        return row[key] if row is not None else None

    INSERT_WALLET_IGNORE = (
        f"INSERT INTO bouzt_wallets (user_id, balance, created_at) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}) "
        "ON CONFLICT(user_id) DO NOTHING"
    )
    INSERT_META_UPSERT = (
        f"INSERT INTO bouzt_meta (key, value) VALUES ({PARAM}, {PARAM}) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    INSERT_COMP_RETURN_ID = (
        f"INSERT INTO bouzt_competitions (title, status, created_by, created_at, locks_at) "
        f"VALUES ({PARAM}, 'open', {PARAM}, {PARAM}, {PARAM})"
    )
    INSERT_OUTCOME_RETURN_ID = (
        f"INSERT INTO bouzt_outcomes (competition_id, label, position) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM})"
    )
    INSERT_BET_RETURN_ID = (
        f"INSERT INTO bouzt_bets (competition_id, outcome_id, user_id, stake, placed_at) "
        f"VALUES ({PARAM}, {PARAM}, {PARAM}, {PARAM}, {PARAM})"
    )


SCHEMA_WALLETS = f"""
CREATE TABLE IF NOT EXISTS bouzt_wallets (
    user_id     {BIGINT} PRIMARY KEY,
    balance     {BIGINT} NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
)
"""

# Note: no CHECK on status — validated in the service layer so we can add
# new states (e.g. 'locked') without an ALTER-CHECK migration dance.
SCHEMA_COMPETITIONS = f"""
CREATE TABLE IF NOT EXISTS bouzt_competitions (
    id                 {SERIAL_PK},
    title              TEXT NOT NULL,
    status             TEXT NOT NULL,
    created_by         {BIGINT} NOT NULL,
    created_at         TEXT NOT NULL,
    locks_at           TEXT,
    locked_at          TEXT,
    ended_at           TEXT,
    winning_outcome_id {BIGINT}
)
"""

SCHEMA_OUTCOMES = f"""
CREATE TABLE IF NOT EXISTS bouzt_outcomes (
    id             {SERIAL_PK},
    competition_id {BIGINT} NOT NULL REFERENCES bouzt_competitions(id) ON DELETE CASCADE,
    label          TEXT NOT NULL,
    position       INTEGER NOT NULL
)
"""

# `result` ∈ {'win','loss','refund','cancelled'} once settled. NULL while open.
SCHEMA_BETS = f"""
CREATE TABLE IF NOT EXISTS bouzt_bets (
    id             {SERIAL_PK},
    competition_id {BIGINT} NOT NULL REFERENCES bouzt_competitions(id) ON DELETE CASCADE,
    outcome_id     {BIGINT} NOT NULL REFERENCES bouzt_outcomes(id),
    user_id        {BIGINT} NOT NULL,
    stake          {BIGINT} NOT NULL CHECK (stake > 0),
    placed_at      TEXT NOT NULL,
    settled        INTEGER NOT NULL DEFAULT 0,
    payout         {BIGINT} NOT NULL DEFAULT 0,
    result         TEXT
)
"""

SCHEMA_META = """
CREATE TABLE IF NOT EXISTS bouzt_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS bouzt_outcomes_comp_idx ON bouzt_outcomes(competition_id)",
    "CREATE INDEX IF NOT EXISTS bouzt_bets_comp_idx ON bouzt_bets(competition_id)",
    "CREATE INDEX IF NOT EXISTS bouzt_bets_user_idx ON bouzt_bets(user_id)",
    "CREATE INDEX IF NOT EXISTS bouzt_bets_outcome_idx ON bouzt_bets(outcome_id)",
    "CREATE INDEX IF NOT EXISTS bouzt_comps_status_idx ON bouzt_competitions(status)",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _column_exists(c, table: str, column: str) -> bool:
    if USE_POSTGRES:
        cur = c.execute(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name = {PARAM} AND column_name = {PARAM}",
            (table, column),
        )
        return cur.fetchone() is not None
    cur = c.execute(f"PRAGMA table_info({table})")
    return any(r[1] == column for r in cur.fetchall())


def _drop_status_check(c) -> None:
    """The previous schema had CHECK (status IN ('open','closed','cancelled'))
    on bouzt_competitions. Drop it so the new 'locked' state is writable on
    databases that were created before this migration."""
    if USE_POSTGRES:
        # Drop every CHECK constraint on bouzt_competitions whose definition
        # references the `status` column. Safe — `stake > 0` lives on
        # bouzt_bets and isn't touched.
        cur = c.execute(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = 'bouzt_competitions' "
            "  AND con.contype = 'c' "
            "  AND pg_get_constraintdef(con.oid) ILIKE '%status%'"
        )
        for row in cur.fetchall():
            name = row["conname"] if isinstance(row, dict) else row[0]
            c.execute(f'ALTER TABLE bouzt_competitions DROP CONSTRAINT IF EXISTS "{name}"')
        return
    # SQLite: CHECK can't be dropped via ALTER. Rewrite the schema text in
    # sqlite_master directly — preferred over a table rebuild because of the
    # ON DELETE CASCADE FKs from bouzt_outcomes and bouzt_bets.
    import re
    cur = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bouzt_competitions'"
    )
    row = cur.fetchone()
    if row is None:
        return
    sql = row[0]
    if sql is None or "CHECK" not in sql.upper():
        return
    new_sql, n = re.subn(
        r"\s*CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    if n == 0:
        return
    c.execute("PRAGMA writable_schema = 1")
    c.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='bouzt_competitions'",
        (new_sql,),
    )
    c.execute("PRAGMA writable_schema = 0")
    # Bump schema_version so the current connection notices the change.
    cur = c.execute("PRAGMA schema_version")
    v = cur.fetchone()[0]
    c.execute(f"PRAGMA schema_version = {int(v) + 1}")


def _migrate(c) -> None:
    """Add columns and drop the obsolete status CHECK on pre-existing tables.
    Idempotent."""
    for col, ddl in [
        ("locks_at", f"ALTER TABLE bouzt_competitions ADD COLUMN locks_at TEXT"),
        ("locked_at", f"ALTER TABLE bouzt_competitions ADD COLUMN locked_at TEXT"),
    ]:
        if not _column_exists(c, "bouzt_competitions", col):
            c.execute(ddl)
    if not _column_exists(c, "bouzt_bets", "result"):
        c.execute("ALTER TABLE bouzt_bets ADD COLUMN result TEXT")
    _drop_status_check(c)


def init_db() -> None:
    log.info("bouzt storage backend: %s", "postgres" if USE_POSTGRES else "sqlite")
    with conn() as c:
        c.execute(SCHEMA_WALLETS)
        c.execute(SCHEMA_COMPETITIONS)
        c.execute(SCHEMA_OUTCOMES)
        c.execute(SCHEMA_BETS)
        c.execute(SCHEMA_META)
        _migrate(c)
        for idx in INDEXES:
            c.execute(idx)


# --- meta -----------------------------------------------------------------

def meta_get(key: str) -> Optional[str]:
    with conn() as c:
        cur = c.execute(f"SELECT value FROM bouzt_meta WHERE key = {PARAM}", (key,))
        row = cur.fetchone()
        return _row_get(row, "value")


def meta_set(key: str, value: str) -> None:
    with conn() as c:
        c.execute(INSERT_META_UPSERT, (key, value))


# --- wallets --------------------------------------------------------------

def get_wallet(user_id: int) -> Optional[dict]:
    with conn() as c:
        cur = c.execute(
            f"SELECT user_id, balance, created_at FROM bouzt_wallets WHERE user_id = {PARAM}",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "user_id": _row_get(row, "user_id"),
            "balance": _row_get(row, "balance"),
            "created_at": _row_get(row, "created_at"),
        }


def grant_starting_balance(user_id: int, amount: int) -> bool:
    """Insert a wallet with the starting balance iff none exists. Returns True
    iff a new row was inserted (i.e. this user had never been granted)."""
    with conn() as c:
        cur = c.execute(INSERT_WALLET_IGNORE, (user_id, amount, _now_iso()))
        return cur.rowcount > 0


def ensure_wallet_in_txn(c, user_id: int, amount: int) -> None:
    """Same as grant_starting_balance but reuses the caller's connection so
    the row is created inside an outer transaction."""
    c.execute(INSERT_WALLET_IGNORE, (user_id, amount, _now_iso()))


def bulk_grant_starting_balance(user_ids, amount: int) -> int:
    inserted = 0
    if not user_ids:
        return 0
    now = _now_iso()
    with conn() as c:
        for uid in user_ids:
            cur = c.execute(INSERT_WALLET_IGNORE, (uid, amount, now))
            if cur.rowcount > 0:
                inserted += 1
    return inserted


def leaderboard(limit: int = 10, offset: int = 0) -> list:
    with conn() as c:
        cur = c.execute(
            "SELECT user_id, balance FROM bouzt_wallets "
            f"ORDER BY balance DESC, user_id ASC LIMIT {PARAM} OFFSET {PARAM}",
            (limit, offset),
        )
        return [dict(user_id=_row_get(r, "user_id"), balance=_row_get(r, "balance"))
                for r in cur.fetchall()]


def wallet_count() -> int:
    with conn() as c:
        cur = c.execute("SELECT COUNT(*) AS n FROM bouzt_wallets")
        row = cur.fetchone()
        return int(_row_get(row, "n") or 0)


# --- competitions / outcomes / bets ---------------------------------------

def create_competition(title: str, created_by: int, outcome_labels, locks_at: Optional[str]) -> dict:
    now = _now_iso()
    with conn() as c:
        cur = c.execute(INSERT_COMP_RETURN_ID, (title, created_by, now, locks_at))
        if USE_POSTGRES:
            comp_id = cur.fetchone()["id"]
        else:
            comp_id = cur.lastrowid
        outcomes = []
        for pos, label in enumerate(outcome_labels):
            cur = c.execute(INSERT_OUTCOME_RETURN_ID, (comp_id, label, pos))
            if USE_POSTGRES:
                oid = cur.fetchone()["id"]
            else:
                oid = cur.lastrowid
            outcomes.append({"id": oid, "label": label, "position": pos})
    return {"id": comp_id, "title": title, "outcomes": outcomes, "locks_at": locks_at}


def get_competition(comp_id: int) -> Optional[dict]:
    with conn() as c:
        cur = c.execute(
            f"SELECT * FROM bouzt_competitions WHERE id = {PARAM}", (comp_id,)
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None


def get_outcomes(comp_id: int) -> list:
    with conn() as c:
        cur = c.execute(
            f"SELECT id, competition_id, label, position FROM bouzt_outcomes "
            f"WHERE competition_id = {PARAM} ORDER BY position ASC",
            (comp_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def list_competitions_by_status(status: str) -> list:
    with conn() as c:
        cur = c.execute(
            f"SELECT * FROM bouzt_competitions WHERE status = {PARAM} "
            "ORDER BY id DESC",
            (status,),
        )
        return [dict(r) for r in cur.fetchall()]


def list_active_competitions() -> list:
    """Open + locked (not yet settled), newest first."""
    with conn() as c:
        cur = c.execute(
            "SELECT * FROM bouzt_competitions WHERE status IN ('open','locked') "
            "ORDER BY id DESC"
        )
        return [dict(r) for r in cur.fetchall()]


def list_recent_competitions_raw(limit: int = 25) -> list:
    with conn() as c:
        cur = c.execute(
            f"SELECT * FROM bouzt_competitions ORDER BY id DESC LIMIT {PARAM}",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def hydrate_competitions(comps: list) -> list:
    """One-shot fill of outcomes + per-outcome pools for a batch of competitions.
    Two queries total instead of 2N."""
    if not comps:
        return comps
    ids = [int(c["id"]) for c in comps]
    placeholders = ",".join([PARAM] * len(ids))
    with conn() as c:
        cur = c.execute(
            f"SELECT id, competition_id, label, position FROM bouzt_outcomes "
            f"WHERE competition_id IN ({placeholders}) "
            f"ORDER BY competition_id ASC, position ASC",
            tuple(ids),
        )
        outcomes_by_comp: dict = {}
        for r in cur.fetchall():
            outcomes_by_comp.setdefault(int(r["competition_id"]), []).append(dict(r))
        cur = c.execute(
            f"SELECT competition_id, outcome_id, COALESCE(SUM(stake), 0) AS pool "
            f"FROM bouzt_bets WHERE competition_id IN ({placeholders}) "
            f"GROUP BY competition_id, outcome_id",
            tuple(ids),
        )
        pools_by_outcome: dict = {}
        for r in cur.fetchall():
            pools_by_outcome[(int(r["competition_id"]), int(r["outcome_id"]))] = int(r["pool"])
    for comp in comps:
        cid = int(comp["id"])
        outs = outcomes_by_comp.get(cid, [])
        for o in outs:
            o["pool"] = int(pools_by_outcome.get((cid, int(o["id"])), 0))
        comp["outcomes"] = outs
        comp["total_pool"] = sum(o["pool"] for o in outs)
    return comps


def per_outcome_pools(comp_id: int) -> dict:
    with conn() as c:
        cur = c.execute(
            f"SELECT outcome_id, COALESCE(SUM(stake), 0) AS pool "
            f"FROM bouzt_bets WHERE competition_id = {PARAM} GROUP BY outcome_id",
            (comp_id,),
        )
        return {_row_get(r, "outcome_id"): int(_row_get(r, "pool"))
                for r in cur.fetchall()}


def bets_for_competition(comp_id: int) -> list:
    with conn() as c:
        cur = c.execute(
            f"SELECT * FROM bouzt_bets WHERE competition_id = {PARAM} ORDER BY id ASC",
            (comp_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def user_bet_history(user_id: int, limit: int, offset: int) -> list:
    with conn() as c:
        cur = c.execute(
            "SELECT b.id, b.competition_id, b.outcome_id, b.stake, b.placed_at, "
            "       b.settled, b.payout, b.result, "
            "       o.label AS outcome_label, "
            "       c.title AS competition_title, c.status AS competition_status, "
            "       c.winning_outcome_id "
            "FROM bouzt_bets b "
            "JOIN bouzt_outcomes o ON o.id = b.outcome_id "
            "JOIN bouzt_competitions c ON c.id = b.competition_id "
            f"WHERE b.user_id = {PARAM} "
            f"ORDER BY b.id DESC LIMIT {PARAM} OFFSET {PARAM}",
            (user_id, limit, offset),
        )
        return [dict(r) for r in cur.fetchall()]


def user_bet_count(user_id: int) -> int:
    with conn() as c:
        cur = c.execute(
            f"SELECT COUNT(*) AS n FROM bouzt_bets WHERE user_id = {PARAM}",
            (user_id,),
        )
        return int(_row_get(cur.fetchone(), "n") or 0)
