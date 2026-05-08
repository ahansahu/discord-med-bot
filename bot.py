import io
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
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


IMPORT_TTL_SECONDS = 300
_import_prompts: dict[int, datetime] = {}


def _register_import_prompt(message_id: int) -> None:
    expiry = datetime.now(timezone.utc) + timedelta(seconds=IMPORT_TTL_SECONDS)
    _import_prompts[message_id] = expiry
    now = datetime.now(timezone.utc)
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


async def _confirm(channel: discord.abc.Messageable, *, already: bool = False) -> None:
    if already:
        await channel.send("👌 already logged for today.")
    else:
        await channel.send("✅ logged! nice one.")


async def _do_mark_taken(channel: discord.abc.Messageable) -> bool:
    today = storage.today_str()
    sticker_idx = chart.random_sticker_index()
    newly = storage.mark_taken(today, sticker_idx)
    await _confirm(channel, already=not newly)
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
    if message.stickers and message.reference and \
            _is_active_import_prompt(message.reference.message_id):
        await _import_stickers(message)
        return
    text = message.content.strip().lower()
    if any(text == w or text.startswith(w + " ") or text.startswith(w + "!") for w in CONFIRM_WORDS):
        await _do_mark_taken(message.channel)
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
    today = storage.today_str()
    expected = storage.get_reminder_msg_id(today)
    if expected is None or payload.message_id != expected:
        return
    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    await _do_mark_taken(channel)


# ---------- slash commands ----------

@bot.tree.command(name="taken", description="Mark today's medication as taken.")
async def cmd_taken(interaction: discord.Interaction) -> None:
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    today = storage.today_str()
    sticker_idx = chart.random_sticker_index()
    newly = storage.mark_taken(today, sticker_idx)
    await interaction.response.send_message(
        "✅ logged! nice one." if newly else "👌 already logged for today."
    )


@bot.tree.command(name="status", description="Show today's medication status.")
async def cmd_status(interaction: discord.Interaction) -> None:
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
    await _send_month(interaction)


@bot.tree.command(name="chart", description="Alias for /month.")
async def cmd_chart(interaction: discord.Interaction) -> None:
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


@bot.tree.command(name="removesticker", description="Delete a custom sticker by filename.")
@app_commands.describe(name="Filename, e.g. custom_001.png")
async def cmd_removesticker(interaction: discord.Interaction, name: str) -> None:
    if not _is_target(interaction.user.id):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True)
        return
    if chart.remove_custom_sticker(name):
        await interaction.response.send_message(
            f"🗑️ removed `{name}`. existing chart entries referencing it will "
            f"wrap around the remaining pool."
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


def main() -> None:
    storage.init_db()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
