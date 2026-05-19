"""Bouzt Gold — currency + pari-mutuel betting feature.

Public API: ``register(bot)``. Call it from your bot's ``on_ready`` handler
(it's idempotent, so reconnects are safe). Reads its config from env vars:
``BOUZT_CHANNEL_ID`` (required), ``GUILD_ID``, ``TARGET_USER_ID``,
``DATABASE_URL`` or ``DB_PATH``.
"""
import asyncio
import logging

from . import db, events
from .commands import bouzt_group
from .config import BOUZT_CHANNEL_ID

log = logging.getLogger("bouzt")


def register(bot) -> None:
    """Wire the Bouzt feature into a discord.py bot. Idempotent across
    reconnects (on_ready can fire many times)."""
    if BOUZT_CHANNEL_ID is None:
        log.warning("BOUZT_CHANNEL_ID unset — Bouzt feature disabled.")
        return

    db.init_db()

    if bot.tree.get_command("bouzt") is None:
        bot.tree.add_command(bouzt_group)

    if not getattr(bot, "_bouzt_listeners_added", False):
        bot.add_listener(events.on_member_join, name="on_member_join")
        bot._bouzt_listeners_added = True

    # Schedule the one-time backfill after this on_ready cycle. Fire-and-forget
    # because it self-guards via the meta flag in the DB.
    if not getattr(bot, "_bouzt_backfill_scheduled", False):
        bot._bouzt_backfill_scheduled = True
        asyncio.create_task(events.run_backfill(bot))


__all__ = ["register"]
