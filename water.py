"""Daily water-intake tracker — a fun, interactive pinned "hydration card" with
quick-log buttons and a live progress bar, backed by a 3-tier sticker chart
(under-50% / 50-99% / goal-met) that mirrors the medication chart.

The pinned card follows the same pattern as todo.py: a persistent
``timeout=None`` view that is re-attached on every rerender/startup, so the
buttons keep working across restarts without separate registration.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

from config import CHANNEL_ID, TZ, WATER_CHANNEL_ID, WATER_DAILY_GOAL
import chart
import storage

log = logging.getLogger("med_bot.water")

PINNED_STATE_KEY = "water_pinned_msg_id"
EMBED_COLOR = discord.Color.from_str("#5AB4E5")
FILLED = "💧"
EMPTY = "⬜"

_TIER_BLURB = {
    0: "let's get hydrated! tap 💧 +1 glass to start.",
    1: "💧 nice start — keep sipping!",
    2: "🌊 halfway there, you've got this!",
    3: "🏆 goal smashed! amazing work 🎉",
}


def _channel_id() -> int:
    return WATER_CHANNEL_ID or CHANNEL_ID


# --- state + rendering ----------------------------------------------------

def _today():
    """Return (date_str, row, streak) for today, creating the row if needed."""
    d = storage.today_str()
    storage.water_ensure(d, WATER_DAILY_GOAL)
    return d, storage.water_get(d), storage.water_streak()


def _progress_bar(glasses: int, goal: int) -> str:
    filled = min(glasses, goal)
    empty = max(0, goal - glasses)
    bar = FILLED * filled + EMPTY * empty
    if glasses > goal:
        bar += "  +" + FILLED * (glasses - goal)
    return bar or EMPTY


def render_embed(row, streak: int) -> discord.Embed:
    glasses = row["glasses"] if row else 0
    goal = row["goal"] if row else WATER_DAILY_GOAL
    tier = storage.water_tier(glasses, goal)
    pct = int(round(100 * glasses / goal)) if goal else 0

    embed = discord.Embed(title="💧 Daily Hydration", color=EMBED_COLOR)
    embed.add_field(
        name=f"{glasses} / {goal} glasses · {pct}%",
        value=_progress_bar(glasses, goal),
        inline=False,
    )
    embed.add_field(name="today", value=_TIER_BLURB[tier], inline=False)
    if streak:
        streak_txt = f"🔥 {streak} day{'s' if streak != 1 else ''} in a row"
    else:
        streak_txt = "no streak yet — hit your goal today!"
    embed.add_field(name="streak", value=streak_txt, inline=False)
    embed.set_footer(text="💧 +1 · ✏️ custom · ↩️ undo · 🔄 reset · 📊 chart")
    return embed


# --- pinned card lifecycle ------------------------------------------------

async def _resolve_channel(bot: commands.Bot) -> Optional[discord.abc.Messageable]:
    cid = _channel_id()
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except discord.HTTPException as e:
            log.warning("could not fetch water channel: %s", e)
            return None
    return channel


async def ensure_pinned(bot: commands.Bot) -> Optional[discord.Message]:
    """Ensure a pinned hydration card exists; create + pin if missing."""
    channel = await _resolve_channel(bot)
    if channel is None:
        return None
    msg_id_str = storage.water_get_state(PINNED_STATE_KEY)
    msg = None
    if msg_id_str:
        try:
            msg = await channel.fetch_message(int(msg_id_str))
        except discord.NotFound:
            msg = None
        except discord.HTTPException as e:
            log.warning("ensure_pinned: fetch failed: %s", e)
            return None
    _, row, streak = _today()
    embed = render_embed(row, streak)
    view = WaterView()
    if msg is None:
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin()
        except discord.HTTPException as e:
            log.warning("could not pin water card: %s", e)
        storage.water_set_state(PINNED_STATE_KEY, str(msg.id))
    else:
        await msg.edit(content=None, embed=embed, view=view)
    return msg


async def rerender(bot: commands.Bot) -> None:
    """Fetch the pinned card and edit it with fresh state."""
    channel = await _resolve_channel(bot)
    if channel is None:
        return
    msg_id_str = storage.water_get_state(PINNED_STATE_KEY)
    if not msg_id_str:
        await ensure_pinned(bot)
        return
    try:
        msg = await channel.fetch_message(int(msg_id_str))
    except discord.NotFound:
        log.info("pinned water card missing; recreating")
        await ensure_pinned(bot)
        return
    except discord.HTTPException as e:
        log.warning("rerender fetch failed: %s", e)
        return
    _, row, streak = _today()
    await msg.edit(content=None, embed=render_embed(row, streak), view=WaterView())


async def _ack_inline(interaction: discord.Interaction) -> None:
    """Edit the card the component is on with fresh state."""
    _, row, streak = _today()
    await interaction.response.edit_message(
        content=None, embed=render_embed(row, streak), view=WaterView()
    )


# --- interactive view -----------------------------------------------------

class WaterView(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(Plus1Button())
        self.add_item(CustomButton())
        self.add_item(UndoButton())
        self.add_item(ResetButton())
        self.add_item(ChartButton())


class Plus1Button(ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.success,
                         label="💧 +1 glass", custom_id="water:plus1")

    async def callback(self, interaction: discord.Interaction) -> None:
        d = storage.today_str()
        storage.water_ensure(d, WATER_DAILY_GOAL)
        storage.water_add(d, 1, WATER_DAILY_GOAL)
        await _ack_inline(interaction)


class CustomButton(ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="✏️ Custom", custom_id="water:custom")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CustomAmountModal())


class UndoButton(ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="↩️ Undo", custom_id="water:undo")

    async def callback(self, interaction: discord.Interaction) -> None:
        d = storage.today_str()
        storage.water_ensure(d, WATER_DAILY_GOAL)
        storage.water_add(d, -1, WATER_DAILY_GOAL)
        await _ack_inline(interaction)


class ResetButton(ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.danger,
                         label="🔄 Reset", custom_id="water:reset")

    async def callback(self, interaction: discord.Interaction) -> None:
        d = storage.today_str()
        row = storage.water_get(d)
        goal = row["goal"] if row else WATER_DAILY_GOAL
        storage.water_set(d, 0, goal)
        await _ack_inline(interaction)


class ChartButton(ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.primary,
                         label="📊 Chart", custom_id="water:chart")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        png = chart.render_water_week_strip()
        f = discord.File(io.BytesIO(png), filename="water_week.png")
        await interaction.followup.send(file=f, ephemeral=True)


class CustomAmountModal(ui.Modal, title="Log water"):
    amount: ui.TextInput = ui.TextInput(
        label="Glasses",
        required=True,
        max_length=6,
        placeholder="2, or +1 / -1 to adjust",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.amount.value).strip()
        d = storage.today_str()
        storage.water_ensure(d, WATER_DAILY_GOAL)
        row = storage.water_get(d)
        goal = row["goal"] if row else WATER_DAILY_GOAL
        try:
            if raw and raw[0] in "+-":
                storage.water_add(d, int(raw), goal)
            else:
                storage.water_set(d, int(raw), goal)
        except ValueError:
            await interaction.response.send_message(
                "please enter a whole number like `2`, `+1`, or `-1`.",
                ephemeral=True,
            )
            return
        await _ack_inline(interaction)


# --- slash commands -------------------------------------------------------

async def _slash_gate(interaction: discord.Interaction) -> bool:
    """Channel guard for water commands. True → reject."""
    if interaction.channel_id != _channel_id():
        await interaction.response.send_message(
            f"please use this in <#{_channel_id()}> 🙏", ephemeral=True
        )
        return True
    return False


def register(bot: commands.Bot) -> None:
    """Register the water slash commands with the bot. Idempotent."""
    if bot.tree.get_command("hydration") is not None:
        return

    @bot.tree.command(name="hydration", description="Show today's water tracker.")
    async def cmd_hydration(interaction: discord.Interaction) -> None:
        if await _slash_gate(interaction):
            return
        _, row, streak = _today()
        await interaction.response.send_message(
            embed=render_embed(row, streak), ephemeral=True
        )
        await rerender(interaction.client)

    @bot.tree.command(name="drink", description="Log glasses of water (default 1).")
    @app_commands.describe(glasses="How many glasses (use a negative number to undo)")
    async def cmd_drink(interaction: discord.Interaction, glasses: int = 1) -> None:
        if await _slash_gate(interaction):
            return
        d = storage.today_str()
        storage.water_ensure(d, WATER_DAILY_GOAL)
        row = storage.water_get(d)
        goal = row["goal"] if row else WATER_DAILY_GOAL
        new = storage.water_add(d, glasses, goal)
        msg = f"💧 logged! you're at **{new}/{goal}** glasses today."
        if storage.water_tier(new, goal) >= 3:
            msg += " 🏆 goal met — nice one!"
        await interaction.response.send_message(msg)
        await rerender(interaction.client)

    @bot.tree.command(name="waterweek", description="Last 7 days of water as a sticker strip.")
    async def cmd_waterweek(interaction: discord.Interaction) -> None:
        if await _slash_gate(interaction):
            return
        await interaction.response.defer()
        png = chart.render_water_week_strip()
        f = discord.File(io.BytesIO(png), filename="water_week.png")
        await interaction.followup.send(file=f)

    @bot.tree.command(name="watermonth", description="This month's water sticker calendar.")
    async def cmd_watermonth(interaction: discord.Interaction) -> None:
        if await _slash_gate(interaction):
            return
        await interaction.response.defer()
        today = datetime.now(TZ).date()
        png = chart.render_water_month(today.year, today.month)
        f = discord.File(io.BytesIO(png), filename=f"water_{today.year}-{today.month:02d}.png")
        await interaction.followup.send(file=f)

    @bot.tree.command(name="watergoal", description="Set today's water goal (glasses).")
    @app_commands.describe(glasses="New daily goal in glasses")
    async def cmd_watergoal(interaction: discord.Interaction, glasses: int) -> None:
        if await _slash_gate(interaction):
            return
        if glasses < 1:
            await interaction.response.send_message(
                "goal must be at least 1 glass.", ephemeral=True
            )
            return
        d = storage.today_str()
        row = storage.water_get(d)
        current = row["glasses"] if row else 0
        storage.water_set(d, current, glasses)
        await interaction.response.send_message(
            f"🎯 today's goal set to **{glasses}** glasses."
        )
        await rerender(interaction.client)
