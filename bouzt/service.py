"""Business logic for Bouzt Gold. Pure (no discord imports) so it stays
testable and easily extractable.

Atomic operations (place_bet, pay, end_competition, cancel_competition,
cancel_bet, lock_competition) open their own connection and perform all
reads + writes inside a single transaction. On Postgres we additionally
lock affected wallet rows with SELECT ... FOR UPDATE.
"""
import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db
from .config import (
    MAX_LOCK_DURATION_MINUTES,
    STARTING_BALANCE,
    TARGET_USER_ID,
    USE_POSTGRES,
)

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


class CompetitionLocked(BouztError):
    pass


class InvalidOutcome(BouztError):
    pass


class InvalidAmount(BouztError):
    pass


class BetNotFound(BouztError):
    pass


# Per-competition locks for end/cancel/lock + place_bet — prevents a settle
# from racing a place_bet on SQLite (which has no FOR UPDATE).
_comp_locks: "defaultdict[int, asyncio.Lock]" = defaultdict(asyncio.Lock)


def _drop_comp_lock(comp_id: int) -> None:
    lock = _comp_locks.get(comp_id)
    if lock is not None and not lock.locked():
        _comp_locks.pop(comp_id, None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _is_admin(user_id: int) -> bool:
    return TARGET_USER_ID is not None and user_id == TARGET_USER_ID


def _require_admin(user_id: int) -> None:
    if not _is_admin(user_id):
        raise NotAdmin("admin only.")


# --- duration parsing -----------------------------------------------------

_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)


def parse_duration_to_minutes(text: str) -> int:
    """'30m' / '2h' / '3d' / '90s' → minutes. Raises BouztError on bad input."""
    text = (text or "").strip()
    if not text:
        raise BouztError("empty duration.")
    m = _DUR_RE.match(text)
    if not m:
        raise BouztError("duration must look like `30m`, `2h`, or `1d`.")
    n = int(m.group(1))
    unit = m.group(2).lower()
    seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    minutes = max(1, seconds // 60)
    if minutes > MAX_LOCK_DURATION_MINUTES:
        raise BouztError(f"duration too long (max {MAX_LOCK_DURATION_MINUTES} minutes).")
    return minutes


# --- wallets --------------------------------------------------------------

def ensure_wallet(user_id: int) -> int:
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
    with db.conn() as c:
        db.ensure_wallet_in_txn(c, target_id, STARTING_BALANCE)
        _fetch_balance_for_update(c, target_id)
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (amount, target_id),
        )
        cur = c.execute(
            f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM}",
            (target_id,),
        )
        return int(db._row_get(cur.fetchone(), "balance"))


def pay(sender_id: int, recipient_id: int, amount: int) -> dict:
    """Transfer between two user wallets, atomically."""
    if amount <= 0:
        raise InvalidAmount("amount must be a positive integer.")
    if sender_id == recipient_id:
        raise BouztError("can't pay yourself.")
    # Lock in consistent order to avoid deadlocks.
    lo, hi = sorted((sender_id, recipient_id))
    with db.conn() as c:
        db.ensure_wallet_in_txn(c, sender_id, STARTING_BALANCE)
        db.ensure_wallet_in_txn(c, recipient_id, STARTING_BALANCE)
        _fetch_balance_for_update(c, lo)
        _fetch_balance_for_update(c, hi)
        sender_bal = _fetch_balance(c, sender_id)
        if sender_bal < amount:
            raise InsufficientFunds(f"not enough Bouzt Gold (balance: {sender_bal}).")
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance - {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (amount, sender_id),
        )
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (amount, recipient_id),
        )
        new_sender = _fetch_balance(c, sender_id)
        new_recipient = _fetch_balance(c, recipient_id)
    return {
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "amount": amount,
        "sender_balance": new_sender,
        "recipient_balance": new_recipient,
    }


def leaderboard(limit: int = 10, offset: int = 0) -> list:
    return db.leaderboard(limit=limit, offset=offset)


def leaderboard_size() -> int:
    return db.wallet_count()


# --- competitions ---------------------------------------------------------

def create_competition(
    actor_id: int,
    title: str,
    outcome_labels,
    lock_in_minutes: Optional[int] = None,
) -> dict:
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
    locks_at = None
    if lock_in_minutes is not None and lock_in_minutes > 0:
        locks_at = (_now() + timedelta(minutes=lock_in_minutes)).isoformat(timespec="seconds")
    return db.create_competition(
        title=title, created_by=actor_id, outcome_labels=labels, locks_at=locks_at,
    )


def list_open_competitions() -> list:
    """Comps still accepting bets (status='open' and not past locks_at)."""
    comps = db.list_active_competitions()
    now_iso = _now_iso()
    comps = [c for c in comps if c["status"] == "open" and (not c.get("locks_at") or c["locks_at"] > now_iso)]
    return db.hydrate_competitions(comps)


def list_active_competitions() -> list:
    """Open + locked, not yet settled. Used for /list and autocomplete."""
    return db.hydrate_competitions(db.list_active_competitions())


def list_recent_competitions(limit: int = 25) -> list:
    return db.hydrate_competitions(db.list_recent_competitions_raw(limit=limit))


def competition_detail(comp_id: int) -> dict:
    comp = db.get_competition(comp_id)
    if comp is None:
        raise CompetitionNotFound(f"no competition with id {comp_id}.")
    return db.hydrate_competitions([comp])[0]


def find_expired_locks() -> list:
    """Returns open competitions whose locks_at <= now. Used by the scheduler."""
    now_iso = _now_iso()
    with db.conn() as c:
        cur = c.execute(
            f"SELECT id, title, locks_at FROM bouzt_competitions "
            f"WHERE status = 'open' AND locks_at IS NOT NULL AND locks_at <= {db.PARAM}",
            (now_iso,),
        )
        return [dict(r) for r in cur.fetchall()]


# --- betting --------------------------------------------------------------

def _is_locked(comp: dict, now_iso: str) -> bool:
    if comp["status"] == "locked":
        return True
    return bool(comp.get("locks_at")) and comp["locks_at"] <= now_iso


async def place_bet(user_id: int, comp_id: int, outcome_id: int, amount: int) -> dict:
    """Atomic. Wrapped in a per-competition asyncio lock so settlement can't
    race a bet on SQLite (where FOR UPDATE is unavailable)."""
    if amount <= 0:
        raise InvalidAmount("bet amount must be a positive integer.")
    async with _comp_locks[comp_id]:
        return _place_bet_sync(user_id, comp_id, outcome_id, amount)


def _place_bet_sync(user_id: int, comp_id: int, outcome_id: int, amount: int) -> dict:
    now_iso = _now_iso()
    with db.conn() as c:
        db.ensure_wallet_in_txn(c, user_id, STARTING_BALANCE)
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] in ("closed", "cancelled"):
            raise CompetitionClosed(f"competition {comp_id} is {comp['status']}.")
        if _is_locked(comp, now_iso):
            raise CompetitionLocked(f"competition {comp_id} is locked — no more bets.")
        cur = c.execute(
            f"SELECT id, label FROM bouzt_outcomes "
            f"WHERE id = {db.PARAM} AND competition_id = {db.PARAM}",
            (outcome_id, comp_id),
        )
        out_row = cur.fetchone()
        if out_row is None:
            raise InvalidOutcome(f"outcome {outcome_id} doesn't belong to competition {comp_id}.")
        outcome_label = db._row_get(out_row, "label")
        balance = _fetch_balance_for_update(c, user_id)
        if balance < amount:
            raise InsufficientFunds(f"not enough Bouzt Gold (balance: {balance}).")
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance - {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (amount, user_id),
        )
        cur = c.execute(
            db.INSERT_BET_RETURN_ID,
            (comp_id, outcome_id, user_id, amount, now_iso),
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


async def cancel_bet(user_id: int, bet_id: int) -> dict:
    """User cancels their own bet on a still-open (un-locked) competition.
    Refunds the stake and marks the bet result='cancelled'."""
    # We don't know the comp_id without a read, but we want the per-comp
    # asyncio lock for end-vs-cancel safety. Two-step: peek then lock.
    with db.conn() as c:
        cur = c.execute(
            f"SELECT id, user_id, competition_id, stake, settled "
            f"FROM bouzt_bets WHERE id = {db.PARAM}",
            (bet_id,),
        )
        row = cur.fetchone()
    if row is None or int(db._row_get(row, "user_id")) != user_id:
        raise BetNotFound(f"no bet {bet_id} that belongs to you.")
    comp_id = int(db._row_get(row, "competition_id"))
    async with _comp_locks[comp_id]:
        return _cancel_bet_sync(user_id, bet_id)


def _cancel_bet_sync(user_id: int, bet_id: int) -> dict:
    now_iso = _now_iso()
    with db.conn() as c:
        cur = c.execute(
            f"SELECT b.id, b.user_id, b.competition_id, b.outcome_id, b.stake, b.settled "
            f"FROM bouzt_bets b WHERE b.id = {db.PARAM}",
            (bet_id,),
        )
        bet = cur.fetchone()
        if bet is None or int(db._row_get(bet, "user_id")) != user_id:
            raise BetNotFound(f"no bet {bet_id} that belongs to you.")
        if int(db._row_get(bet, "settled")):
            raise BouztError("that bet is already settled.")
        comp_id = int(db._row_get(bet, "competition_id"))
        stake = int(db._row_get(bet, "stake"))
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"competition {comp_id} vanished.")
        if comp["status"] in ("closed", "cancelled"):
            raise CompetitionClosed(f"competition {comp_id} is {comp['status']}.")
        if _is_locked(comp, now_iso):
            raise CompetitionLocked("can't cancel — betting is already locked.")
        db.ensure_wallet_in_txn(c, user_id, STARTING_BALANCE)
        _fetch_balance_for_update(c, user_id)
        c.execute(
            f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
            f"WHERE user_id = {db.PARAM}",
            (stake, user_id),
        )
        c.execute(
            f"UPDATE bouzt_bets SET settled = 1, payout = {db.PARAM}, result = 'cancelled' "
            f"WHERE id = {db.PARAM}",
            (stake, bet_id),
        )
        new_balance = _fetch_balance(c, user_id)
    return {
        "bet_id": bet_id,
        "competition_id": comp_id,
        "stake": stake,
        "new_balance": new_balance,
    }


def my_bets(user_id: int, comp_id: int) -> list:
    """Caller's bets for one competition (un-settled or settled)."""
    with db.conn() as c:
        cur = c.execute(
            "SELECT b.*, o.label AS outcome_label FROM bouzt_bets b "
            "JOIN bouzt_outcomes o ON o.id = b.outcome_id "
            f"WHERE b.user_id = {db.PARAM} AND b.competition_id = {db.PARAM} "
            "ORDER BY b.id ASC",
            (user_id, comp_id),
        )
        return [dict(r) for r in cur.fetchall()]


def bet_history(user_id: int, limit: int = 10, offset: int = 0) -> list:
    return db.user_bet_history(user_id, limit, offset)


def bet_history_size(user_id: int) -> int:
    return db.user_bet_count(user_id)


# --- locking / ending / cancelling ----------------------------------------

async def lock_competition(actor_id: Optional[int], comp_id: int, *, system: bool = False) -> dict:
    """Lock a competition so no more bets can be placed. `system=True` is used
    by the auto-lock scheduler and bypasses the admin check."""
    if not system:
        _require_admin(actor_id)
    async with _comp_locks[comp_id]:
        return _lock_competition_sync(comp_id)


def _lock_competition_sync(comp_id: int) -> dict:
    now_iso = _now_iso()
    with db.conn() as c:
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] in ("closed", "cancelled"):
            raise CompetitionClosed(f"competition {comp_id} is {comp['status']}.")
        if comp["status"] == "locked":
            return {"competition_id": comp_id, "title": comp["title"], "already": True}
        c.execute(
            f"UPDATE bouzt_competitions SET status = 'locked', locked_at = {db.PARAM} "
            f"WHERE id = {db.PARAM}",
            (now_iso, comp_id),
        )
    return {"competition_id": comp_id, "title": comp["title"], "already": False}


async def end_competition(actor_id: int, comp_id: int, winning_outcome_id: int) -> dict:
    _require_admin(actor_id)
    async with _comp_locks[comp_id]:
        result = _end_competition_sync(comp_id, winning_outcome_id)
    _drop_comp_lock(comp_id)
    return result


async def cancel_competition(actor_id: int, comp_id: int) -> dict:
    _require_admin(actor_id)
    async with _comp_locks[comp_id]:
        result = _cancel_competition_sync(comp_id)
    _drop_comp_lock(comp_id)
    return result


def _end_competition_sync(comp_id: int, winning_outcome_id: int) -> dict:
    now_iso = _now_iso()
    with db.conn() as c:
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] in ("closed", "cancelled"):
            raise CompetitionClosed(f"competition {comp_id} is already {comp['status']}.")
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
        cur = c.execute(
            f"SELECT id, user_id, outcome_id, stake FROM bouzt_bets "
            f"WHERE competition_id = {db.PARAM} AND (settled = 0 OR settled IS NULL) "
            f"ORDER BY id ASC",
            (comp_id,),
        )
        all_bets = [dict(r) for r in cur.fetchall()]
        total_pool = sum(int(b["stake"]) for b in all_bets)
        winning_bets = [b for b in all_bets if int(b["outcome_id"]) == int(winning_outcome_id)]
        losing_bets = [b for b in all_bets if int(b["outcome_id"]) != int(winning_outcome_id)]
        winners_pool = sum(int(b["stake"]) for b in winning_bets)
        losers_pool = sum(int(b["stake"]) for b in losing_bets)

        refunded = False
        payouts = []  # list of (bet_id, user_id, stake, payout, result)
        if not all_bets:
            pass
        elif winners_pool == 0:
            refunded = True
            for b in all_bets:
                payouts.append((b["id"], b["user_id"], int(b["stake"]), int(b["stake"]), "refund"))
        else:
            for b in winning_bets:
                share = (int(b["stake"]) * total_pool) // winners_pool
                payouts.append((b["id"], b["user_id"], int(b["stake"]), share, "win"))
            for b in losing_bets:
                payouts.append((b["id"], b["user_id"], int(b["stake"]), 0, "loss"))

        unique_users = sorted({uid for (_bid, uid, _s, _p, _r) in payouts})
        for uid in unique_users:
            db.ensure_wallet_in_txn(c, uid, STARTING_BALANCE)
            _fetch_balance_for_update(c, uid)

        for (bet_id, uid, _stake, payout, res) in payouts:
            if payout > 0:
                c.execute(
                    f"UPDATE bouzt_wallets SET balance = balance + {db.PARAM} "
                    f"WHERE user_id = {db.PARAM}",
                    (payout, uid),
                )
            c.execute(
                f"UPDATE bouzt_bets SET settled = 1, payout = {db.PARAM}, result = {db.PARAM} "
                f"WHERE id = {db.PARAM}",
                (payout, res, bet_id),
            )

        c.execute(
            f"UPDATE bouzt_competitions SET status = 'closed', ended_at = {db.PARAM}, "
            f"winning_outcome_id = {db.PARAM} WHERE id = {db.PARAM}",
            (now_iso, winning_outcome_id, comp_id),
        )

    allocated = sum(p for (_b, _u, _s, p, _r) in payouts)
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
        "losers_pool": losers_pool,
        "bet_count": len(all_bets),
        "winner_count": len(winning_bets),
        "refunded": refunded,
        "remainder": remainder,
        "payouts": [
            {"bet_id": bid, "user_id": uid, "stake": stake, "payout": payout, "result": res}
            for (bid, uid, stake, payout, res) in payouts
        ],
    }


def _cancel_competition_sync(comp_id: int) -> dict:
    now_iso = _now_iso()
    with db.conn() as c:
        comp = _fetch_comp_for_update(c, comp_id)
        if comp is None:
            raise CompetitionNotFound(f"no competition with id {comp_id}.")
        if comp["status"] in ("closed", "cancelled"):
            raise CompetitionClosed(f"competition {comp_id} is already {comp['status']}.")
        cur = c.execute(
            f"SELECT id, user_id, stake FROM bouzt_bets "
            f"WHERE competition_id = {db.PARAM} AND (settled = 0 OR settled IS NULL) "
            f"ORDER BY id ASC",
            (comp_id,),
        )
        bets = [dict(r) for r in cur.fetchall()]
        unique_users = sorted({int(b["user_id"]) for b in bets})
        for uid in unique_users:
            db.ensure_wallet_in_txn(c, uid, STARTING_BALANCE)
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
                f"UPDATE bouzt_bets SET settled = 1, payout = {db.PARAM}, result = 'refund' "
                f"WHERE id = {db.PARAM}",
                (stake, int(b["id"])),
            )
        c.execute(
            f"UPDATE bouzt_competitions SET status = 'cancelled', ended_at = {db.PARAM} "
            f"WHERE id = {db.PARAM}",
            (now_iso, comp_id),
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
        sql = f"SELECT * FROM bouzt_competitions WHERE id = {db.PARAM} FOR UPDATE"
    else:
        sql = f"SELECT * FROM bouzt_competitions WHERE id = {db.PARAM}"
    cur = c.execute(sql, (comp_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _fetch_balance_for_update(c, user_id: int) -> int:
    if USE_POSTGRES:
        sql = f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM} FOR UPDATE"
    else:
        sql = f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM}"
    cur = c.execute(sql, (user_id,))
    row = cur.fetchone()
    if row is None:
        return 0
    return int(db._row_get(row, "balance"))


def _fetch_balance(c, user_id: int) -> int:
    cur = c.execute(
        f"SELECT balance FROM bouzt_wallets WHERE user_id = {db.PARAM}",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    return int(db._row_get(row, "balance"))
