import io
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    CHANNEL_ID,
    CONFIRM_REACTIONS,
    CONFIRM_WORDS,
    DISCORD_TOKEN,
    GUILD_ID,
    TARGET_USER_ID,
    TZ,
)
import chart
import storage
from scheduler import setup_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("med_bot")


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)


def _is_target(user_id: int) -> bool:
    return user_id == TARGET_USER_ID


async def _wrong_channel(interaction: discord.Interaction) -> bool:
    """Reject slash commands invoked outside the configured channel."""
    if interaction.channel_id == CHANNEL_ID:
        return False
    await interaction.response.send_message(
        f"please use this command in <#{CHANNEL_ID}> 🙏",
        ephemeral=True,
    )
    return True


async def _confirm(channel: discord.abc.Messageable, *, already: bool = False) -> None:
    if already:
        await channel.send("👌 already logged for today.")
    else:
        await channel.send("✅ logged! nice one.")


async def _edit_reminder_to_logged(
    bot_client: discord.Client, med_day: str
) -> None:
    """Edit the original reminder message to reflect the logged state."""
    msg_id = storage.get_reminder_msg_id(med_day)
    if msg_id is None:
        return
    channel = bot_client.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot_client.fetch_channel(CHANNEL_ID)
        except discord.HTTPException as e:
            log.warning("could not fetch channel for reminder edit: %s", e)
            return
    try:
        msg = await channel.fetch_message(msg_id)
        when = datetime.now(TZ).strftime("%H:%M")
        await msg.edit(
            content=f"✅ Medication logged at {when} — no more reminders today."
        )
    except discord.HTTPException as e:
        log.warning("could not edit reminder message %s: %s", msg_id, e)


async def _do_mark_taken(
    channel: discord.abc.Messageable, bot_client: discord.Client
) -> bool:
    med_day = storage.medication_day_str()
    if not storage.is_pending(med_day):
        row = storage.get_status(med_day)
        if row is None:
            await channel.send("no active reminder right now — nothing to log.")
        elif row["status"] == "taken":
            await _confirm(channel, already=True)
        else:
            await channel.send("that reminder already expired (marked missed).")
        return False
    sticker_idx = chart.random_sticker_index()
    newly = storage.mark_taken(med_day, sticker_idx)
    await _confirm(channel, already=not newly)
    if newly:
        await _edit_reminder_to_logged(bot_client, med_day)
    return newly


# ---------- events ----------

@bot.event
async def on_ready() -> None:
    log.info("logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    storage.init_db()
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("synced %d guild commands", len(synced))
    except Exception as e:
        log.warning("guild sync failed, falling back to global: %s", e)
        synced = await bot.tree.sync()
        log.info("synced %d global commands", len(synced))

    if not getattr(bot, "_scheduler_started", False):
        bot._scheduler = setup_scheduler(bot)
        bot._scheduler.start()
        bot._scheduler_started = True
        log.info("scheduler started; jobs=%s",
                 [j.id for j in bot._scheduler.get_jobs()])


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        await bot.process_commands(message)
        return
    if not _is_target(message.author.id):
        await bot.process_commands(message)
        return
    text = message.content.strip().lower()
    if any(text == w or text.startswith(w + " ") or text.startswith(w + "!") for w in CONFIRM_WORDS):
        await _do_mark_taken(message.channel, bot)
    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.channel_id != CHANNEL_ID:
        return
    if not _is_target(payload.user_id):
        return
    if str(payload.emoji) not in CONFIRM_REACTIONS:
        return
    med_day = storage.medication_day_str()
    expected = storage.get_reminder_msg_id(med_day)
    if expected is None or payload.message_id != expected:
        return
    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    await _do_mark_taken(channel, bot)


# ---------- slash commands ----------

@bot.tree.command(name="taken", description="Mark today's medication as taken.")
async def cmd_taken(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    med_day = storage.medication_day_str()
    existing = storage.get_status(med_day)
    if existing is not None and existing["status"] == "missed":
        await interaction.response.send_message(
            "that day was already marked missed — can't log it retroactively."
        )
        return
    sticker_idx = chart.random_sticker_index()
    newly = storage.mark_taken(med_day, sticker_idx)
    await interaction.response.send_message(
        "✅ logged! nice one." if newly else "👌 already logged for today."
    )
    if newly:
        await _edit_reminder_to_logged(bot, med_day)


@bot.tree.command(name="status", description="Show today's medication status.")
async def cmd_status(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    today = storage.today_str()
    row = storage.get_status(today)
    if row is None:
        msg = "no entry for today yet — first reminder fires at 11:00."
    elif row["status"] == "taken":
        when = row["taken_at"]
        try:
            t = datetime.fromisoformat(when).strftime("%H:%M")
        except (TypeError, ValueError):
            t = "?"
        msg = f"✅ taken today at {t}"
    elif row["status"] == "missed":
        msg = "❌ marked as missed for today."
    else:
        msg = "⏳ pending — reply `yes` or react ✅ on the reminder."
    streak = storage.current_streak()
    msg += f"\ncurrent streak: **{streak}** day{'s' if streak != 1 else ''}"
    await interaction.response.send_message(msg)


@bot.tree.command(name="week", description="Last-7-days summary with sticker strip.")
async def cmd_week(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    await interaction.response.defer(thinking=True)
    png = chart.render_week_strip()
    file = discord.File(io.BytesIO(png), filename="week.png")
    end = datetime.now(TZ).date()
    start = end - timedelta(days=6)
    cnt = storage.counts(start, end)
    streak = storage.current_streak()
    summary = (
        f"**Last 7 days** ({start.isoformat()} → {end.isoformat()})\n"
        f"taken **{cnt['taken']}** · missed **{cnt['missed']}** · pending **{cnt['pending']}**\n"
        f"streak: **{streak}**"
    )
    await interaction.followup.send(content=summary, file=file)


async def _send_month(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    today = datetime.now(TZ).date()
    png = chart.render_month(today.year, today.month)
    file = discord.File(io.BytesIO(png), filename=f"{today.year}-{today.month:02d}.png")
    from datetime import date as date_cls
    import calendar as _cal
    start = date_cls(today.year, today.month, 1)
    end = date_cls(today.year, today.month, _cal.monthrange(today.year, today.month)[1])
    cnt = storage.counts(start, end)
    streak = storage.current_streak()
    summary = (
        f"**{_cal.month_name[today.month]} {today.year}**\n"
        f"taken **{cnt['taken']}** · missed **{cnt['missed']}** · pending **{cnt['pending']}**\n"
        f"streak: **{streak}**"
    )
    await interaction.followup.send(content=summary, file=file)


@bot.tree.command(name="month", description="This month's sticker chart and summary.")
async def cmd_month(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    await _send_month(interaction)


@bot.tree.command(name="chart", description="Alias for /month.")
async def cmd_chart(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    await _send_month(interaction)


def main() -> None:
    storage.init_db()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
