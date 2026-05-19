"""Slash command surface for /bouzt. All commands run user input through the
service layer; service raises BouztError subclasses which are rendered as
ephemeral replies.
"""
import logging

import discord
from discord import app_commands

from . import service
from .config import (
    BOUZT_CHANNEL_ID,
    CURRENCY_NAME,
    CURRENCY_SHORT,
    TARGET_USER_ID,
)

log = logging.getLogger("bouzt.commands")


bouzt_group = app_commands.Group(
    name="bouzt",
    description=f"{CURRENCY_NAME} currency and pari-mutuel betting.",
)

comp_group = app_commands.Group(
    name="competition",
    description="Manage betting competitions.",
    parent=bouzt_group,
)


# --- gating helpers -------------------------------------------------------

async def _gate(interaction: discord.Interaction) -> bool:
    """Returns True if the command should proceed. Replies ephemerally and
    returns False otherwise."""
    if BOUZT_CHANNEL_ID is None:
        await interaction.response.send_message(
            "Bouzt is not configured (missing BOUZT_CHANNEL_ID).",
            ephemeral=True,
        )
        return False
    if interaction.channel_id != BOUZT_CHANNEL_ID:
        await interaction.response.send_message(
            f"please use this command in <#{BOUZT_CHANNEL_ID}> 🙏",
            ephemeral=True,
        )
        return False
    # Lazy wallet creation as defence-in-depth alongside on_member_join.
    try:
        service.ensure_wallet(interaction.user.id)
    except Exception as e:
        log.warning("ensure_wallet failed for %s: %s", interaction.user.id, e)
    return True


def _is_admin(user_id: int) -> bool:
    return TARGET_USER_ID is not None and user_id == TARGET_USER_ID


async def _reply_error(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _fmt(amount: int) -> str:
    return f"{amount:,} {CURRENCY_SHORT}"


def _user_label(guild: discord.Guild | None, user_id: int) -> str:
    if guild is not None:
        m = guild.get_member(user_id)
        if m is not None:
            return m.display_name
    return f"user {user_id}"


# --- autocomplete ---------------------------------------------------------

async def _open_comp_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    try:
        comps = service.list_open_competitions()
    except Exception as e:
        log.warning("autocomplete open comps failed: %s", e)
        return []
    q = (current or "").lower()
    out = []
    for comp in comps:
        label = f"#{comp['id']} — {comp['title']}"
        if q and q not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=int(comp["id"])))
        if len(out) >= 25:
            break
    return out


async def _any_comp_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    try:
        comps = service.list_recent_competitions(limit=50)
    except Exception as e:
        log.warning("autocomplete recent comps failed: %s", e)
        return []
    q = (current or "").lower()
    out = []
    for comp in comps:
        label = f"#{comp['id']} [{comp['status']}] {comp['title']}"
        if q and q not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=int(comp["id"])))
        if len(out) >= 25:
            break
    return out


async def _outcomes_for_comp_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    comp_id = getattr(interaction.namespace, "competition_id", None)
    if comp_id is None:
        return []
    try:
        comp = service.competition_detail(int(comp_id))
    except Exception:
        return []
    q = (current or "").lower()
    out = []
    for o in comp["outcomes"]:
        label = f"#{int(o['position']) + 1} — {o['label']}"
        if q and q not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=int(o["id"])))
        if len(out) >= 25:
            break
    return out


# --- /bouzt balance -------------------------------------------------------

@bouzt_group.command(name="balance", description="Show a user's Bouzt Gold balance.")
@app_commands.describe(user="Defaults to you.")
async def cmd_balance(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
):
    if not await _gate(interaction):
        return
    target = user or interaction.user
    try:
        bal = service.get_balance(target.id)
    except Exception as e:
        log.exception("balance failed")
        await _reply_error(interaction, f"error: {e}")
        return
    if target.id == interaction.user.id:
        msg = f"you have **{_fmt(bal)}**."
    else:
        msg = f"{target.mention} has **{_fmt(bal)}**."
    await interaction.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


# --- /bouzt leaderboard ---------------------------------------------------

@bouzt_group.command(name="leaderboard", description=f"Top 10 {CURRENCY_NAME} holders.")
async def cmd_leaderboard(interaction: discord.Interaction):
    if not await _gate(interaction):
        return
    rows = service.leaderboard(limit=10)
    if not rows:
        await interaction.response.send_message("no wallets yet.")
        return
    lines = []
    for i, row in enumerate(rows, 1):
        name = _user_label(interaction.guild, int(row["user_id"]))
        lines.append(f"`{i:>2}.` **{name}** — {_fmt(int(row['balance']))}")
    embed = discord.Embed(
        title=f"{CURRENCY_NAME} leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


# --- /bouzt give ----------------------------------------------------------

@bouzt_group.command(name="give", description="(admin) Mint Bouzt Gold to a user.")
@app_commands.describe(user="Recipient.", amount="Positive integer to mint.")
async def cmd_give(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
):
    if not await _gate(interaction):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    try:
        new_balance = service.admin_give(interaction.user.id, user.id, amount)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("give failed")
        await _reply_error(interaction, f"error: {e}")
        return
    await interaction.response.send_message(
        f"minted **{_fmt(amount)}** to {user.mention}. new balance: **{_fmt(new_balance)}**.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


# --- /bouzt bet -----------------------------------------------------------

@bouzt_group.command(name="bet", description="Bet on a competition outcome.")
@app_commands.describe(
    competition_id="ID from /bouzt competition list.",
    outcome_id="ID of the outcome you're betting on.",
    amount="Stake (positive integer).",
)
@app_commands.autocomplete(
    competition_id=_open_comp_autocomplete,
    outcome_id=_outcomes_for_comp_autocomplete,
)
async def cmd_bet(
    interaction: discord.Interaction,
    competition_id: int,
    outcome_id: int,
    amount: int,
):
    if not await _gate(interaction):
        return
    try:
        result = service.place_bet(interaction.user.id, competition_id, outcome_id, amount)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("bet failed")
        await _reply_error(interaction, f"error: {e}")
        return
    await interaction.response.send_message(
        f"{interaction.user.mention} bet **{_fmt(result['stake'])}** on "
        f"**{result['outcome_label']}** in #{result['competition_id']} "
        f"({result['competition_title']}). balance: **{_fmt(result['new_balance'])}**.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


# --- /bouzt competition create -------------------------------------------

@comp_group.command(name="create", description="(admin) Create a new competition.")
@app_commands.describe(
    title="Short title shown in lists.",
    outcomes="Comma-separated outcome labels, e.g. 'Team A, Team B, Draw'.",
)
async def cmd_create(
    interaction: discord.Interaction,
    title: str,
    outcomes: str,
):
    if not await _gate(interaction):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    labels = [s for s in (x.strip() for x in outcomes.split(",")) if s]
    try:
        comp = service.create_competition(interaction.user.id, title, labels)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("create failed")
        await _reply_error(interaction, f"error: {e}")
        return
    lines = [
        f"`#{int(o['position']) + 1}` — {o['label']}"
        for o in comp["outcomes"]
    ]
    embed = discord.Embed(
        title=f"Competition #{comp['id']} — {comp['title']}",
        description=(
            "bets are now open. use `/bouzt bet` and pick the outcome from "
            "the autocomplete."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(name="Outcomes", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)


# --- /bouzt competition list ---------------------------------------------

@comp_group.command(name="list", description="List open competitions.")
async def cmd_list(interaction: discord.Interaction):
    if not await _gate(interaction):
        return
    comps = service.list_open_competitions()
    if not comps:
        await interaction.response.send_message("no open competitions.")
        return
    embeds = []
    for comp in comps[:10]:
        embed = _competition_embed(comp)
        embeds.append(embed)
    await interaction.response.send_message(embeds=embeds)


# --- /bouzt competition info ---------------------------------------------

@comp_group.command(name="info", description="Show details of a competition.")
@app_commands.describe(competition_id="ID of the competition.")
@app_commands.autocomplete(competition_id=_any_comp_autocomplete)
async def cmd_info(interaction: discord.Interaction, competition_id: int):
    if not await _gate(interaction):
        return
    try:
        comp = service.competition_detail(competition_id)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    await interaction.response.send_message(embed=_competition_embed(comp))


def _competition_embed(comp: dict) -> discord.Embed:
    status = comp.get("status", "?")
    color = {
        "open": discord.Color.green(),
        "closed": discord.Color.dark_grey(),
        "cancelled": discord.Color.red(),
    }.get(status, discord.Color.blurple())
    embed = discord.Embed(
        title=f"#{comp['id']} — {comp['title']}",
        description=f"status: **{status}**  •  pool: **{_fmt(int(comp.get('total_pool', 0)))}**",
        color=color,
    )
    rows = []
    for o in comp["outcomes"]:
        marker = ""
        if status == "closed" and comp.get("winning_outcome_id") == o["id"]:
            marker = " 🏆"
        rows.append(
            f"`#{int(o['position']) + 1}` {o['label']} — "
            f"{_fmt(int(o.get('pool', 0)))}{marker}"
        )
    embed.add_field(name="Outcomes", value="\n".join(rows) or "(none)", inline=False)
    return embed


# --- /bouzt competition end ----------------------------------------------

@comp_group.command(name="end", description="(admin) End a competition by declaring the winner.")
@app_commands.describe(
    competition_id="ID of the competition to settle.",
    winning_outcome_id="The winning outcome ID.",
)
@app_commands.autocomplete(
    competition_id=_open_comp_autocomplete,
    winning_outcome_id=_outcomes_for_comp_autocomplete,
)
async def cmd_end(
    interaction: discord.Interaction,
    competition_id: int,
    winning_outcome_id: int,
):
    if not await _gate(interaction):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    await interaction.response.defer()
    try:
        result = await service.end_competition(
            interaction.user.id, competition_id, winning_outcome_id
        )
    except service.BouztError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    except Exception as e:
        log.exception("end failed")
        await interaction.followup.send(f"error: {e}", ephemeral=True)
        return

    color = discord.Color.orange() if result["refunded"] else discord.Color.gold()
    title = f"Competition #{result['competition_id']} — {result['title']}"
    if result["refunded"]:
        desc = (
            f"winner: **{result['winning_outcome_label']}** (no one bet on it)\n"
            f"refunded **{result['bet_count']}** bets totalling "
            f"**{_fmt(result['total_pool'])}**."
        )
    elif result["bet_count"] == 0:
        desc = "no bets were placed. closed with no payouts."
    else:
        desc = (
            f"winner: **{result['winning_outcome_label']}**\n"
            f"pool: **{_fmt(result['total_pool'])}**  •  "
            f"winners' stake: **{_fmt(result['winners_pool'])}**\n"
            f"bets: **{result['bet_count']}**"
            + (f"  •  unallocated remainder: {_fmt(result['remainder'])}"
               if result["remainder"] else "")
        )
    embed = discord.Embed(title=title, description=desc, color=color)
    # Winners breakdown (only if real payouts, not refund).
    if not result["refunded"] and result["bet_count"] > 0:
        winners = [p for p in result["payouts"] if p["payout"] > 0]
        if winners:
            lines = []
            for p in winners[:25]:
                name = _user_label(interaction.guild, int(p["user_id"]))
                lines.append(
                    f"**{name}** — staked {_fmt(p['stake'])} → won **{_fmt(p['payout'])}**"
                )
            embed.add_field(name="Payouts", value="\n".join(lines), inline=False)
    await interaction.followup.send(embed=embed)


# --- /bouzt competition cancel -------------------------------------------

@comp_group.command(name="cancel", description="(admin) Cancel a competition and refund every bet.")
@app_commands.describe(competition_id="ID of the competition to cancel.")
@app_commands.autocomplete(competition_id=_open_comp_autocomplete)
async def cmd_cancel(interaction: discord.Interaction, competition_id: int):
    if not await _gate(interaction):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    await interaction.response.defer()
    try:
        result = await service.cancel_competition(interaction.user.id, competition_id)
    except service.BouztError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    except Exception as e:
        log.exception("cancel failed")
        await interaction.followup.send(f"error: {e}", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"Competition #{result['competition_id']} cancelled",
        description=(
            f"refunded **{result['bet_count']}** bets totalling "
            f"**{_fmt(result['refunded_total'])}**."
        ),
        color=discord.Color.red(),
    )
    await interaction.followup.send(embed=embed)
