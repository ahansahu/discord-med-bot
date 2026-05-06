import io
import logging
from datetime import date as date_cls, datetime, timedelta

import aiohttp
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import (
    CHANNEL_ID,
    DB_PATH,
    HEARTBEAT_INTERVAL_MIN,
    MISSED_CUTOFF_HOUR,
    REMINDER_HOURS,
    TARGET_USER_ID,
    TZ,
    UPTIMEROBOT_HEARTBEAT_URL,
    WEEKLY_BACKUP_DOW,
    WEEKLY_BACKUP_HOUR,
    WEEKLY_SUMMARY_DOW,
    WEEKLY_SUMMARY_HOUR,
)
import chart
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
    target = (now.date() - timedelta(days=1)).isoformat()
    if storage.mark_missed_if_pending(target):
        try:
            channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
            await channel.send(MISSED_NOTICE)
        except discord.HTTPException as e:
            log.warning("failed to post missed notice: %s", e)


async def _heartbeat() -> None:
    if not UPTIMEROBOT_HEARTBEAT_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(UPTIMEROBOT_HEARTBEAT_URL, timeout=10) as resp:
                if resp.status >= 400:
                    log.warning("heartbeat returned %s", resp.status)
    except Exception as e:
        log.warning("heartbeat failed: %s", e)


async def _weekly_summary(bot: discord.Client) -> None:
    """Post a weekly check-in — also serves as a passive 'I'm alive' signal."""
    try:
        channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        png = chart.render_week_strip()
        f = discord.File(io.BytesIO(png), filename="week.png")
        end = datetime.now(TZ).date()
        start = end - timedelta(days=6)
        cnt = storage.counts(start, end)
        streak = storage.current_streak()
        msg = (
            f"📊 **Weekly check-in** — taken **{cnt['taken']}** / 7, "
            f"missed **{cnt['missed']}**, streak **{streak}**.\n"
            f"_(also: bot is alive ✓)_"
        )
        await channel.send(content=msg, file=f)
    except Exception as e:
        log.warning("weekly summary failed: %s", e)


async def _weekly_backup(bot: discord.Client) -> None:
    """DM the configured user a copy of the SQLite DB."""
    try:
        if not DB_PATH.exists():
            log.info("no DB file yet, skipping backup")
            return
        user = bot.get_user(TARGET_USER_ID) or await bot.fetch_user(TARGET_USER_ID)
        if user is None:
            log.warning("could not resolve TARGET_USER_ID for backup")
            return
        today = date_cls.today().isoformat()
        f = discord.File(str(DB_PATH), filename=f"med_bot_backup_{today}.db")
        await user.send(
            content="📦 weekly backup of your medication log. Save this file — "
                    "drop it in next to `bot.py` as `med_bot.db` if you ever "
                    "need to restore.",
            file=f,
        )
    except discord.Forbidden:
        log.warning("backup DM forbidden — user must share a server with the bot "
                    "and have DMs from server members enabled")
    except Exception as e:
        log.warning("weekly backup failed: %s", e)


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

    # Mitigation 1: external heartbeat (optional, only if URL set)
    if UPTIMEROBOT_HEARTBEAT_URL:
        sched.add_job(
            _heartbeat,
            IntervalTrigger(minutes=HEARTBEAT_INTERVAL_MIN, timezone=TZ),
            id="heartbeat",
            replace_existing=True,
            misfire_grace_time=120,
            next_run_time=datetime.now(TZ) + timedelta(seconds=10),
        )

    # Mitigation 2: weekly summary post (visible heartbeat)
    sched.add_job(
        _weekly_summary,
        CronTrigger(day_of_week=WEEKLY_SUMMARY_DOW,
                    hour=WEEKLY_SUMMARY_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": bot},
        id="weekly_summary",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Mitigation 3: weekly DB backup via DM
    sched.add_job(
        _weekly_backup,
        CronTrigger(day_of_week=WEEKLY_BACKUP_DOW,
                    hour=WEEKLY_BACKUP_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": bot},
        id="weekly_backup",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    return sched
