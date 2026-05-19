"""Business logic for Bouzt Gold. Pure (no discord imports) so it stays
testable and easily extractable.

Atomic operations (place_bet, end_competition, cancel_competition) open their
own connection and perform all reads + writes inside a single transaction.
On Postgres we additionally lock affected wallet rows with SELECT ... FOR
UPDATE to be safe against concurrent admin actions. On SQLite there's only
one writer at a time anyway.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .config import STARTING_BALANCE, TARGET_USER_ID, USE_POSTGRES

log = logging.getLogger("bouzt.service")


class BouztError(Exception):
    """User-facing error. The message is safe to show in Discord."""


class NotAdmin(BouztError):
    pass


class InsufficientFunds(BouztError):
    pass


class CompetitionNotFound(BouztError):
    pass


class CompetitionClosed(BouztError):
    pass


class InvalidOutcome(BouztError):
    pass


class InvalidAmount(BouztError):
    pass


# Per-competition locks for end/cancel; prevents an admin double-click from
# racing the payout against itself.
_comp_locks: "defaultdict[int, asyncio.Lock]" = defaultdict(asyncio.Lock)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_admin(user_id: int) -> bool:
    return TARGET_USER_ID is not None and user_id == TARGET_USER_ID


def _require_admin(user_id: int) -> None:
    if not _is_admin(user_id):
        raise NotAdmin("admin only.")


# --- wallets --------------------------------------------------------------

def ensure_wallet(user_id: int) -> int:
    """Idempotent: grants STARTING_BALANCE iff the user has no wallet yet.
    Returns the user's current balance."""
    db.grant_starting_balance(user_id, STARTING_BALANCE)
    w = db.get_wallet(user_id)
    return int(w["balance"]) if w else 0


def get_balance(user_id: int) -> int:
    w = db.get_wallet(user_id)
    if w is None:
        return ensure_wallet(user_id)
    return int(w["balance"])


def admin_give(actor_id: int, target_id: int, amount: int) -> int:
    _require_admin(actor_id)
    if amount <= 0:
        raise InvalidAmount("amount must be a positive integer.")
    ensure_wallet(target_id)
    with db.conn() as c:
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (amount, target_id),
        )
        cur = c.execute(
            f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM}",
            (target_id,),
        )
        row = cur.fetchone()
        return int(db._row_get(row, "balance"))


def leaderboard(limit: int = 10) -> list:
    return db.leaderboard(limit=limit)


# --- competitions ---------------------------------------------------------

def create_competition(actor_id: int, title: str, outcome_labels) -> dict:
    _require_admin(actor_id)
    title = (title or "").strip()
    if not title:
        raise BouztError("title cannot be empty.")
    if len(title) > 200:
        raise BouztError("title too long (max 200 chars).")
    labels = [l.strip() for l in outcome_labels if l and l.strip()]
    if len(labels) < 2:
        raise BouztError("a competition needs at least 2 outcomes.")
    if len(labels) > 25:
        raise BouztError("a competition can have at most 25 outcomes.")
    if len(set(labels)) != len(labels):
        raise BouztError("outcome labels must be unique.")
    return db.create_competition(title=title, created_by=actor_id, outcome_labels=labels)


def list_open_competitions() -> list:
    return _list_with_outcomes_and_pools("open")


def list_recent_competitions(limit: int = 25) -> list:
    """All statuses, most recent first. Used for autocomplete on info/cancel."""
    with db.conn() as c:
        cur = c.execute(
            "SELECT * FROM bouzt_competitions ORDER BY id DESC LIMIT "
            f"{db.PARAM}",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    out = []
    for row in rows:
        outcomes = db.get_outcomes(row["id"])
        pools = db.per_outcome_pools(row["id"])
        for o in outcomes:
            o["pool"] = int(pools.get(o["id"], 0))
        row["outcomes"] = outcomes
        row["total_pool"] = sum(o["pool"] for o in outcomes)
        out.append(row)
    return out


def _list_with_outcomes_and_pools(status: str) -> list:
    rows = db.list_competitions_by_status(status)
    for row in rows:
        outcomes = db.get_outcomes(row["id"])
        pools = db.per_outcome_pools(row["id"])
        for o in outcomes:
            o["pool"] = int(pools.get(o["id"], 0))
        row["outcomes"] = outcomes
        row["total_pool"] = sum(o["pool"] for o in outcomes)
    return rows


def competition_detail(comp_id: int) -> dict:
    comp = db.get_competition(comp_id)
    if comp is None:
        raise CompetitionNotFound(f"no competition with id {comp_id}.")
    outcomes = db.get_outcomes(comp_id)
    pools = db.per_outcome_pools(comp_id)
    for o in outcomes:
        o["pool"] = int(pools.get(o["id"], 0))
    comp["outcomes"] = outcomes
    comp["total_pool"] = sum(o["pool"] for o in outcomes)
    return comp


# --- betting --------------------------------------------------------------

def place_bet(user_id: int, comp_id: int, outcome_id: int, amount: int) -> dict:
    """Atomic: deduct stake from wallet + insert bet row."""
    if amount <= 0:
        raise InvalidAmount("bet amount must be a positive integer.")
    ensure_wallet(user_id)
    now = _now_iso()
    with db.conn() as c:
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] != "open":
            raise CompetitionClosed(
                f"competition {comp_id} is {comp['status']}, no more bets."
            )
        # Validate outcome belongs to this competition.
        cur = c.execute(
            f"SELECT id, label FROM bouzt_outcomes "
            f"WHERE id = {db.PARAM} AND competition_id = {db.PARAM}",
            (outcome_id, comp_id),
        )
        out_row = cur.fetchone()
        if out_row is None:
            raise InvalidOutcome(
                f"outcome {outcome_id} doesn't belong to competition {comp_id}."
            )
        outcome_label = db._row_get(out_row, "label")
        # Lock the wallet row and check funds.
        balance = _fetch_balance_for_update(c, user_id)
        if balance < amount:
            raise InsufficientFunds(
                f"not enough Bouzt Gold (balance: {balance})."
            )
        # Deduct + insert.
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance - {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (amount, user_id),
        )
        cur = c.execute(
            db.INSERT_BET_RETURN_ID,
            (comp_id, outcome_id, user_id, amount, now),
        )
        if USE_POSTGRES:
            bet_id = cur.fetchone()["id"]
        else:
            bet_id = cur.lastrowid
    return {
        "bet_id": bet_id,
        "competition_id": comp_id,
        "competition_title": comp["title"],
        "outcome_id": outcome_id,
        "outcome_label": outcome_label,
        "stake": amount,
        "new_balance": balance - amount,
    }


def my_bets(user_id: int, comp_id: int) -> list:
    with db.conn() as c:
        cur = c.execute(
            "SELECT b.*, o.label AS outcome_label FROM bouzt_bets b "
            "JOIN bouzt_outcomes o ON o.id = b.outcome_id "
            f"WHERE b.user_id = {db.PARAM} AND b.competition_id = {db.PARAM} "
            "ORDER BY b.id ASC",
            (user_id, comp_id),
        )
        return [dict(r) for r in cur.fetchall()]


# --- ending / cancelling --------------------------------------------------

async def end_competition(actor_id: int, comp_id: int, winning_outcome_id: int) -> dict:
    _require_admin(actor_id)
    async with _comp_locks[comp_id]:
        return _end_competition_sync(comp_id, winning_outcome_id)


async def cancel_competition(actor_id: int, comp_id: int) -> dict:
    _require_admin(actor_id)
    async with _comp_locks[comp_id]:
        return _cancel_competition_sync(comp_id)


def _end_competition_sync(comp_id: int, winning_outcome_id: int) -> dict:
    """Pari-mutuel payout. Returns a settlement summary."""
    now = _now_iso()
    with db.conn() as c:
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] != "open":
            raise CompetitionClosed(
                f"competition {comp_id} is already {comp['status']}."
            )
        # Validate the winning outcome belongs to this competition.
        cur = c.execute(
            f"SELECT id, label FROM bouzt_outcomes "
            f"WHERE id = {db.PARAM} AND competition_id = {db.PARAM}",
            (winning_outcome_id, comp_id),
        )
        win_row = cur.fetchone()
        if win_row is None:
            raise InvalidOutcome(
                f"outcome {winning_outcome_id} doesn't belong to competition {comp_id}."
            )
        winning_label = db._row_get(win_row, "label")
        # All bets on this competition.
        cur = c.execute(
            f"SELECT id, user_id, outcome_id, stake FROM bouzt_bets "
            f"WHERE competition_id = {db.PARAM} ORDER BY id ASC",
            (comp_id,),
        )
        all_bets = [dict(r) for r in cur.fetchall()]
        total_pool = sum(int(b["stake"]) for b in all_bets)
        winning_bets = [b for b in all_bets if int(b["outcome_id"]) == int(winning_outcome_id)]
        winners_pool = sum(int(b["stake"]) for b in winning_bets)

        refunded = False
        payouts = []  # list of (bet_id, user_id, stake, payout)
        if not all_bets:
            # No bets at all — just close.
            pass
        elif winners_pool == 0:
            # No one bet on the winning outcome → refund every bet.
            refunded = True
            for b in all_bets:
                payouts.append((b["id"], b["user_id"], int(b["stake"]), int(b["stake"])))
        else:
            for b in winning_bets:
                share = (int(b["stake"]) * total_pool) // winners_pool
                payouts.append((b["id"], b["user_id"], int(b["stake"]), share))

        # Lock all affected wallets (ascending user_id) before crediting.
        unique_users = sorted({uid for (_bid, uid, _stake, _p) in payouts})
        for uid in unique_users:
            ensure_wallet(uid)
            _fetch_balance_for_update(c, uid)

        # Credit payouts + mark bets settled.
        for (bet_id, uid, _stake, payout) in payouts:
            if payout > 0:
                c.execute(
                    f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
                    f"WHERE user_id = {db.PARAM}",
                    (payout, uid),
                )
            c.execute(
                f"UPDATE bouzt_bets SET settled = 1, payout = {db.PARAM} "
                f"WHERE id = {db.PARAM}",
                (payout, bet_id),
            )

        # Update competition status.
        c.execute(
            f"UPDATE bouzt_competitions SET status = 'closed', ended_at = {db.PARAM}, "
            f"winning_outcome_id = {db.PARAM} WHERE id = {db.PARAM}",
            (now, winning_outcome_id, comp_id),
        )

    allocated = sum(p for (_b, _u, _s, p) in payouts)
    remainder = total_pool - allocated if not refunded else 0
    if remainder:
        log.info(
            "competition %d: %d BG unallocated (floor-division remainder)",
            comp_id, remainder,
        )
    return {
        "competition_id": comp_id,
        "title": comp["title"],
        "winning_outcome_id": winning_outcome_id,
        "winning_outcome_label": winning_label,
        "total_pool": total_pool,
        "winners_pool": winners_pool,
        "bet_count": len(all_bets),
        "refunded": refunded,
        "remainder": remainder,
        "payouts": [
            {"bet_id": bid, "user_id": uid, "stake": stake, "payout": payout}
            for (bid, uid, stake, payout) in payouts
        ],
    }


def _cancel_competition_sync(comp_id: int) -> dict:
    now = _now_iso()
    with db.conn() as c:
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] != "open":
            raise CompetitionClosed(
                f"competition {comp_id} is already {comp['status']}."
            )
        cur = c.execute(
            f"SELECT id, user_id, stake FROM bouzt_bets "
            f"WHERE competition_id = {db.PARAM} ORDER BY id ASC",
            (comp_id,),
        )
        bets = [dict(r) for r in cur.fetchall()]
        unique_users = sorted({int(b["user_id"]) for b in bets})
        for uid in unique_users:
            ensure_wallet(uid)
            _fetch_balance_for_update(c, uid)
        total_refund = 0
        for b in bets:
            stake = int(b["stake"])
            total_refund += stake
            c.execute(
                f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
                f"WHERE user_id = {db.PARAM}",
                (stake, int(b["user_id"])),
            )
            c.execute(
                f"UPDATE bouzt_bets SET settled = 1, payout = {db.PARAM} "
                f"WHERE id = {db.PARAM}",
                (stake, int(b["id"])),
            )
        c.execute(
            f"UPDATE bouzt_competitions SET status = 'cancelled', ended_at = {db.PARAM} "
            f"WHERE id = {db.PARAM}",
            (now, comp_id),
        )
    return {
        "competition_id": comp_id,
        "title": comp["title"],
        "refunded_total": total_refund,
        "bet_count": len(bets),
    }


# --- low-level row-locking helpers ----------------------------------------

def _fetch_comp_for_update(c, comp_id: int):
    if USE_POSTGRES:
        sql = (
            f"SELECT * FROM bouzt_competitions WHERE id = {db.PARAM} FOR UPDATE"
        )
    else:
        sql = f"SELECT * FROM bouzt_competitions WHERE id = {db.PARAM}"
    cur = c.execute(sql, (comp_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _fetch_balance_for_update(c, user_id: int) -> int:
    if USE_POSTGRES:
        sql = (
            f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM} FOR UPDATE"
        )
    else:
        sql = f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM}"
    cur = c.execute(sql, (user_id,))
    row = cur.fetchone()
    if row is None:
        return 0
    return int(db._row_get(row, "balance"))
