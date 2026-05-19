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
        f"INSERT INTO bouzt_competitions (title, status, created_by, created_at) "
        f"VALUES ({PARAM}, 'open', {PARAM}, {PARAM}) RETURNING id"
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
        f"INSERT INTO bouzt_competitions (title, status, created_by, created_at) "
        f"VALUES ({PARAM}, 'open', {PARAM}, {PARAM})"
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

SCHEMA_COMPETITIONS = f"""
CREATE TABLE IF NOT EXISTS bouzt_competitions (
    id                 {SERIAL_PK},
    title              TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('open','closed','cancelled')),
    created_by         {BIGINT} NOT NULL,
    created_at         TEXT NOT NULL,
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

SCHEMA_BETS = f"""
CREATE TABLE IF NOT EXISTS bouzt_bets (
    id             {SERIAL_PK},
    competition_id {BIGINT} NOT NULL REFERENCES bouzt_competitions(id) ON DELETE CASCADE,
    outcome_id     {BIGINT} NOT NULL REFERENCES bouzt_outcomes(id),
    user_id        {BIGINT} NOT NULL,
    stake          {BIGINT} NOT NULL CHECK (stake > 0),
    placed_at      TEXT NOT NULL,
    settled        INTEGER NOT NULL DEFAULT 0,
    payout         {BIGINT} NOT NULL DEFAULT 0
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
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    log.info("bouzt storage backend: %s", "postgres" if USE_POSTGRES else "sqlite")
    with conn() as c:
        c.execute(SCHEMA_WALLETS)
        c.execute(SCHEMA_COMPETITIONS)
        c.execute(SCHEMA_OUTCOMES)
        c.execute(SCHEMA_BETS)
        c.execute(SCHEMA_META)
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


def bulk_grant_starting_balance(user_ids, amount: int) -> int:
    """Bulk insert; idempotent. Returns the number of NEW wallets inserted."""
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


def leaderboard(limit: int = 10) -> list:
    with conn() as c:
        cur = c.execute(
            "SELECT user_id, balance FROM bouzt_wallets "
            f"ORDER BY balance DESC, user_id ASC LIMIT {PARAM}",
            (limit,),
        )
        return [dict(user_id=_row_get(r, "user_id"), balance=_row_get(r, "balance"))
                for r in cur.fetchall()]


# --- competitions / outcomes / bets ---------------------------------------

def create_competition(title: str, created_by: int, outcome_labels) -> dict:
    """Insert a competition + its outcomes atomically. Returns
    {id, outcomes: [{id, label, position}]}."""
    now = _now_iso()
    with conn() as c:
        cur = c.execute(INSERT_COMP_RETURN_ID, (title, created_by, now))
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
    return {"id": comp_id, "title": title, "outcomes": outcomes}


def get_competition(comp_id: int) -> Optional[dict]:
    with conn() as c:
        cur = c.execute(
            f"SELECT * FROM bouzt_competitions WHERE id = {PARAM}", (comp_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row) if not USE_POSTGRES else dict(row)


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


def per_outcome_pools(comp_id: int) -> dict:
    """Returns {outcome_id: total_stake} for one competition."""
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
