"""Discord event listeners for the Bouzt Gold feature:
 - on_member_join: grant the starting balance to a new member (idempotent)
 - on_ready backfill: one-time grant to every existing guild member
"""
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from . import db, service
from .config import GUILD_ID, STARTING_BALANCE

log = logging.getLogger("bouzt.events")


def _backfill_key() -> str:
    return f"backfill:{GUILD_ID}"


async def on_member_join(member: discord.Member) -> None:
    if GUILD_ID is not None and member.guild.id != GUILD_ID:
        return
    if member.bot:
        return
    try:
        newly = db.grant_starting_balance(member.id, STARTING_BALANCE)
    except Exception as e:
        log.warning("failed to grant starting balance to %s: %s", member.id, e)
        return
    if newly:
        log.info(
            "granted starting balance to %s (id=%s)", member, member.id
        )


async def run_backfill(bot: commands.Bot) -> None:
    """One-time backfill of every existing guild member. Idempotent: the meta
    flag is checked first, and bulk_grant uses INSERT … ON CONFLICT DO NOTHING
    so even if the flag is cleared, existing wallets aren't disturbed."""
    if GUILD_ID is None:
        log.warning("bouzt backfill: GUILD_ID unset, skipping.")
        return
    flag = db.meta_get(_backfill_key())
    if flag is not None:
        log.info("bouzt backfill: already done (%s)", flag)
        return
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        log.warning("bouzt backfill: guild %s not found in cache", GUILD_ID)
        return
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            log.warning("bouzt backfill: guild.chunk() failed: %s", e)
            return
    user_ids = [m.id for m in guild.members if not m.bot]
    inserted = db.bulk_grant_starting_balance(user_ids, STARTING_BALANCE)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.meta_set(
        _backfill_key(),
        f"done@{stamp}|members={len(user_ids)}|new_wallets={inserted}",
    )
    log.info(
        "bouzt backfill: granted %d new wallets across %d members",
        inserted, len(user_ids),
    )
