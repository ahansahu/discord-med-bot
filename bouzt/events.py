"""Discord event listeners and background tasks for the Bouzt Gold feature:
 - on_member_join: grant the starting balance to a new member (idempotent)
 - on_ready backfill: one-time grant to every existing guild member
 - auto-lock ticker: lock competitions whose locks_at has elapsed
"""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from . import db, service
from .config import (
    AUTO_LOCK_POLL_SECONDS,
    BOUZT_CHANNEL_ID,
    CURRENCY_EMOJI,
    GUILD_ID,
    STARTING_BALANCE,
)

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
        log.info("granted starting balance to %s (id=%s)", member, member.id)


async def run_backfill(bot: commands.Bot) -> None:
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


async def auto_lock_ticker(bot: commands.Bot) -> None:
    """Polls for competitions whose locks_at has elapsed, locks them, and
    posts a notice in the Bouzt channel."""
    await bot.wait_until_ready()
    log.info("bouzt auto-lock ticker started (every %ss)", AUTO_LOCK_POLL_SECONDS)
    while not bot.is_closed():
        try:
            expired = service.find_expired_locks()
            for comp in expired:
                try:
                    result = await service.lock_competition(None, int(comp["id"]), system=True)
                except Exception as e:
                    log.warning("auto-lock %s failed: %s", comp["id"], e)
                    continue
                if result.get("already"):
                    continue
                await _announce_locked(bot, int(comp["id"]), comp["title"])
        except Exception:
            log.exception("auto-lock ticker iteration failed")
        await asyncio.sleep(AUTO_LOCK_POLL_SECONDS)


async def _announce_locked(bot: commands.Bot, comp_id: int, title: str) -> None:
    if BOUZT_CHANNEL_ID is None:
        return
    channel = bot.get_channel(BOUZT_CHANNEL_ID)
    if channel is None:
        return
    try:
        embed = discord.Embed(
            title=f"{CURRENCY_EMOJI}  Betting locked",
            description=(
                f"**#{comp_id} — {title}**\n"
                f"the timer ran out — no more bets. waiting on the result."
            ),
            color=discord.Color.dark_orange(),
        )
        await channel.send(embed=embed)
    except Exception as e:
        log.warning("auto-lock announce failed for %s: %s", comp_id, e)
