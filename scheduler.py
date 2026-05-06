import logging
from datetime import datetime

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    CHANNEL_ID,
    MISSED_CUTOFF_HOUR,
    REMINDER_HOURS,
    TARGET_USER_ID,
    TZ,
)
import storage

log = logging.getLogger("med_bot.scheduler")


REMINDER_TEXT = (
    "<@{user_id}> 💊 time for your medication! "
    "React with ✅ or reply `yes` once you've taken it."
)
NUDGE_TEXT = (
    "<@{user_id}> 🔔 just a nudge — still need to log your medication for today. "
    "React with ✅ or reply `yes` when done."
)
MISSED_NOTICE = (
    "📋 today's medication wasn't logged before the cutoff. Marked as missed."
)


async def _send_reminder(bot: discord.Client, *, is_first: bool) -> None:
    today = storage.today_str()
    if is_first:
        storage.ensure_day(today)
    if not storage.is_pending(today):
        log.info("skip reminder: %s already %s", today,
                 storage.get_status(today)["status"] if storage.get_status(today) else "?")
        return
    channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    text = (REMINDER_TEXT if is_first else NUDGE_TEXT).format(user_id=TARGET_USER_ID)
    msg = await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    storage.set_reminder_msg(today, msg.id)
    try:
        await msg.add_reaction("✅")
    except discord.HTTPException as e:
        log.warning("failed to add ✅ reaction: %s", e)


async def _cutoff_check(bot: discord.Client) -> None:
    # Run at 02:00 — checks the *previous* calendar day in our timezone.
    now = datetime.now(TZ)
    yesterday = (now.date().toordinal() - 1)
    from datetime import date as date_cls
    target = date_cls.fromordinal(yesterday).isoformat()
    if storage.mark_missed_if_pending(target):
        try:
            channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
            await channel.send(MISSED_NOTICE)
        except discord.HTTPException as e:
            log.warning("failed to post missed notice: %s", e)


def setup_scheduler(bot: discord.Client) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=TZ)

    first_hour = REMINDER_HOURS[0]
    for hour in REMINDER_HOURS:
        is_first = hour == first_hour
        sched.add_job(
            _send_reminder,
            CronTrigger(hour=hour, minute=0, timezone=TZ),
            kwargs={"bot": bot, "is_first": is_first},
            id=f"reminder_{hour}",
            replace_existing=True,
            misfire_grace_time=300,
        )

    sched.add_job(
        _cutoff_check,
        CronTrigger(hour=MISSED_CUTOFF_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": bot},
        id="cutoff",
        replace_existing=True,
        misfire_grace_time=600,
    )

    return sched
