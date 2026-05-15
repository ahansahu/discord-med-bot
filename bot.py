import io
import logging
import random
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    CHANNEL_ID,
    CONFIRM_REACTIONS,
    DISCORD_TOKEN,
    GUILD_ID,
    REMINDER_HOURS,
    STICKER_DIR,
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


IMPORT_TTL_SECONDS = 300
_import_prompts: dict[int, datetime] = {}


def _register_import_prompt(message_id: int) -> None:
    now = datetime.now(timezone.utc)
    _import_prompts[message_id] = now + timedelta(seconds=IMPORT_TTL_SECONDS)
    for mid in [m for m, exp in _import_prompts.items() if exp < now]:
        _import_prompts.pop(mid, None)


def _is_active_import_prompt(message_id: int) -> bool:
    exp = _import_prompts.get(message_id)
    if exp is None:
        return False
    if exp < datetime.now(timezone.utc):
        _import_prompts.pop(message_id, None)
        return False
    return True


async def _fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            r.raise_for_status()
            return await r.read()


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


async def _edit_reminder_to_logged(med_day: str) -> None:
    msg_id = storage.get_reminder_msg_id(med_day)
    if msg_id is None:
        return
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
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


async def _do_mark_taken(channel: discord.abc.Messageable) -> bool:
    med_day = storage.medication_day_str()
    row = storage.get_status(med_day)
    if row is None:
        await channel.send("no active reminder right now — nothing to log.")
        return False
    if row["status"] == "taken":
        await _confirm(channel, already=True)
        return False
    if row["status"] == "missed":
        await channel.send("that reminder already expired (marked missed).")
        return False
    sticker_idx = chart.random_sticker_index()
    newly = storage.mark_taken(med_day, sticker_idx)
    await _confirm(channel, already=not newly)
    if newly:
        await _edit_reminder_to_logged(med_day)
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
    if message.channel.id == CHANNEL_ID and _is_target(message.author.id):
        if message.stickers and message.reference and \
                _is_active_import_prompt(message.reference.message_id):
            await _import_stickers(message)
            return
    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.channel_id != CHANNEL_ID:
        return
    if not _is_target(payload.user_id):
        return
    if _is_active_import_prompt(payload.message_id):
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        await _import_emoji_reaction(channel, payload)
        return
    if str(payload.emoji) not in CONFIRM_REACTIONS:
        return
    med_day = storage.medication_day_str()
    expected = storage.get_reminder_msg_id(med_day)
    if expected is None or payload.message_id != expected:
        return
    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    await _do_mark_taken(channel)


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
        await _edit_reminder_to_logged(med_day)


def _next_reminder_str() -> str:
    now = datetime.now(TZ)
    upcoming = next((h for h in REMINDER_HOURS if h > now.hour), None)
    if upcoming is not None:
        return f"{upcoming:02d}:00"
    return f"{REMINDER_HOURS[0]:02d}:00 tomorrow"


@bot.tree.command(name="status", description="Show today's medication status.")
async def cmd_status(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    today = storage.medication_day_str()
    row = storage.get_status(today)
    if row is None:
        msg = f"no entry for today yet — next reminder at {_next_reminder_str()}."
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
        msg = f"⏳ pending — use `/taken` or react ✅ on the reminder. next nudge at {_next_reminder_str()}."
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


@bot.tree.command(name="addsticker", description="Upload a custom sticker image.")
@app_commands.describe(image="Image file (any format) — auto-resized to 96x96.")
async def cmd_addsticker(
    interaction: discord.Interaction, image: discord.Attachment
) -> None:
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    ctype = image.content_type or ""
    if not ctype.startswith("image/"):
        await interaction.response.send_message(
            f"that doesn't look like an image (content-type: `{ctype or 'unknown'}`).",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        data = await image.read()
        path = chart.save_custom_sticker(data)
    except ValueError as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return
    pool_size = len(chart._sticker_pool())
    await interaction.followup.send(
        f"✅ saved as `{path.name}`. sticker pool now has **{pool_size}** designs."
    )


@bot.tree.command(name="liststickers", description="List built-in and custom stickers.")
async def cmd_liststickers(interaction: discord.Interaction) -> None:
    customs = chart.list_custom_stickers()
    lines = [f"**built-in:** sticker_0..{chart.STICKER_COUNT - 1} ({chart.STICKER_COUNT})"]
    if customs:
        lines.append(f"**custom ({len(customs)}):**")
        lines.extend(f"• `{p.name}`" for p in customs)
    else:
        lines.append("**custom:** none yet — use `/addsticker` to add one.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="getsticker", description="Send a sticker image as a file.")
@app_commands.describe(name="Filename e.g. custom_001.png — omit for the most recent custom sticker.")
async def cmd_getsticker(interaction: discord.Interaction, name: str | None = None) -> None:
    if name is None:
        customs = chart.list_custom_stickers()
        if not customs:
            await interaction.response.send_message(
                "no custom stickers yet — use `/addsticker` to upload one.",
                ephemeral=True,
            )
            return
        path = customs[-1]
    else:
        if "/" in name or "\\" in name or ".." in name:
            await interaction.response.send_message("invalid filename.", ephemeral=True)
            return
        path = STICKER_DIR / name
        if not path.is_file():
            await interaction.response.send_message(
                f"`{name}` not found — use `/liststickers` to see available names.",
                ephemeral=True,
            )
            return
    await interaction.response.send_message(file=discord.File(path, filename=path.name))


@bot.tree.command(name="removesticker", description="Delete a custom sticker by filename.")
@app_commands.describe(name="Filename, e.g. custom_001.png")
async def cmd_removesticker(interaction: discord.Interaction, name: str) -> None:
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    if chart.remove_custom_sticker(name):
        await interaction.response.send_message(
            f"🗑️ removed `{name}`. any past chart entries that referenced it "
            f"will fall back to the first built-in sticker."
        )
    else:
        await interaction.response.send_message(
            f"couldn't remove `{name}` — must be an existing `custom_*.png` file.",
            ephemeral=True,
        )


async def _import_emoji_reaction(
    channel: discord.abc.Messageable, payload: discord.RawReactionActionEvent
) -> None:
    emoji = payload.emoji
    if not emoji.is_custom_emoji():
        await channel.send("unicode emojis aren't supported — use a custom server emoji.")
        return
    try:
        data = await _fetch_bytes(str(emoji.url))
        path = chart.save_custom_sticker(data)
    except (aiohttp.ClientError, ValueError) as e:
        await channel.send(f"❌ couldn't import `:{emoji.name}:` — {e}")
        return
    pool_size = len(chart._sticker_pool())
    await channel.send(
        f"✅ saved `{path.name}` from emoji `:{emoji.name}:` (pool now {pool_size})."
    )


async def _import_stickers(message: discord.Message) -> None:
    saved: list[str] = []
    skipped: list[str] = []
    for sticker in message.stickers:
        fmt = sticker.format
        if fmt == discord.StickerFormatType.lottie:
            skipped.append(f"`{sticker.name}` (lottie unsupported)")
            continue
        try:
            data = await _fetch_bytes(str(sticker.url))
            path = chart.save_custom_sticker(data)
        except (aiohttp.ClientError, ValueError) as e:
            skipped.append(f"`{sticker.name}` ({e})")
            continue
        saved.append(path.name)
    parts = []
    if saved:
        pool_size = len(chart._sticker_pool())
        parts.append(f"✅ saved {len(saved)} as " + ", ".join(f"`{n}`" for n in saved) +
                     f" (pool now {pool_size}).")
    if skipped:
        parts.append("skipped: " + ", ".join(skipped))
    await message.channel.send("\n".join(parts) or "nothing imported.")


@bot.tree.command(
    name="importsticker",
    description="Import a Discord emoji (react) or sticker (reply) as a custom sticker.",
)
async def cmd_importsticker(interaction: discord.Interaction) -> None:
    if await _wrong_channel(interaction):
        return
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    await interaction.response.send_message(
        "react to this message with a custom emoji to import it, "
        "or reply to it with a sticker. (within 5 minutes)"
    )
    msg = await interaction.original_response()
    _register_import_prompt(msg.id)


@bot.tree.command(
    name="updatestickers",
    description="Replace built-in stickers in past logs with your custom stickers.",
)
async def cmd_updatestickers(interaction: discord.Interaction) -> None:
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    pool = chart._sticker_pool()
    if len(pool) <= chart.STICKER_COUNT:
        await interaction.response.send_message(
            "no custom stickers found — upload some with `/addsticker` first.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    dates = storage.get_taken_builtin_dates(chart.STICKER_COUNT)
    if not dates:
        await interaction.followup.send(
            "all past entries already use custom stickers — nothing to update."
        )
        return
    for d in dates:
        new_index = random.randrange(chart.STICKER_COUNT, len(pool))
        storage.update_sticker_index(d, new_index)
    n = len(dates)
    await interaction.followup.send(
        f"✅ updated **{n}** past {'entry' if n == 1 else 'entries'} "
        f"to use your custom stickers."
    )


def main() -> None:
    storage.init_db()
    chart.hydrate_custom_stickers()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
