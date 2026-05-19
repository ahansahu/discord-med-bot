"""Slash command surface for /bouzt. All commands run user input through the
service layer; service raises BouztError subclasses which are rendered as
ephemeral replies.
"""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

from . import service
from .config import (
    BOUZT_CHANNEL_ID,
    CURRENCY_EMOJI,
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


# Commands that don't move money — can be used in any channel.
_READ_ONLY = {"balance", "leaderboard", "history", "list", "info", "help"}


# --- gating helpers -------------------------------------------------------

async def _gate(interaction: discord.Interaction, *, write: bool) -> bool:
    """Returns True if the command should proceed. Replies ephemerally and
    returns False otherwise. Read-only commands skip the channel restriction."""
    if BOUZT_CHANNEL_ID is None:
        await interaction.response.send_message(
            "Bouzt is not configured (missing BOUZT_CHANNEL_ID).",
            ephemeral=True,
        )
        return False
    if write and interaction.channel_id != BOUZT_CHANNEL_ID:
        await interaction.response.send_message(
            f"please use this command in <#{BOUZT_CHANNEL_ID}> 🙏",
            ephemeral=True,
        )
        return False
    return True


def _is_admin(user_id: int) -> bool:
    return TARGET_USER_ID is not None and user_id == TARGET_USER_ID


async def _reply_error(interaction: discord.Interaction, message: str) -> None:
    msg = f"⚠️  {message}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _fmt(amount: int) -> str:
    return f"**{amount:,}** {CURRENCY_EMOJI}"


def _fmt_plain(amount: int) -> str:
    return f"{amount:,} {CURRENCY_SHORT}"


def _user_label(guild: discord.Guild | None, user_id: int) -> str:
    if guild is not None:
        m = guild.get_member(user_id)
        if m is not None:
            return m.display_name
    return f"user {user_id}"


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _ts_relative(s: str | None) -> str:
    """Discord relative timestamp from an ISO string. Returns '' if unparseable."""
    dt = _parse_iso(s)
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"


def _status_badge(comp: dict, now_iso: str) -> tuple[str, discord.Color]:
    status = comp.get("status", "?")
    if status == "open":
        locks_at = comp.get("locks_at")
        if locks_at and locks_at <= now_iso:
            return "🔒 locked", discord.Color.dark_orange()
        if locks_at:
            return f"🟢 open · locks {_ts_relative(locks_at)}", discord.Color.green()
        return "🟢 open", discord.Color.green()
    if status == "locked":
        return "🔒 locked", discord.Color.dark_orange()
    if status == "closed":
        return "🏁 closed", discord.Color.dark_grey()
    if status == "cancelled":
        return "✖️ cancelled", discord.Color.red()
    return status, discord.Color.blurple()


def _odds(outcome_pool: int, total_pool: int) -> str:
    if outcome_pool <= 0 or total_pool <= 0:
        return "—"
    return f"{total_pool / outcome_pool:.2f}×"


# --- autocomplete ---------------------------------------------------------

async def _open_comp_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    try:
        comps = service.list_open_competitions()
    except Exception as e:
        log.warning("autocomplete open comps failed: %s", e)
        return []
    return _comp_choices(comps, current, show_status=False)


async def _active_comp_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Open + locked, for /end."""
    try:
        comps = service.list_active_competitions()
    except Exception as e:
        log.warning("autocomplete active comps failed: %s", e)
        return []
    return _comp_choices(comps, current, show_status=True)


async def _any_comp_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    try:
        comps = service.list_recent_competitions(limit=50)
    except Exception as e:
        log.warning("autocomplete recent comps failed: %s", e)
        return []
    return _comp_choices(comps, current, show_status=True)


def _comp_choices(comps, current, *, show_status):
    q = (current or "").lower()
    out = []
    for comp in comps:
        if show_status:
            label = f"#{comp['id']} [{comp['status']}] {comp['title']}"
        else:
            label = f"#{comp['id']} — {comp['title']}"
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


async def _my_open_bet_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    try:
        rows = service.bet_history(interaction.user.id, limit=50, offset=0)
    except Exception:
        return []
    q = (current or "").lower()
    out = []
    for r in rows:
        if int(r.get("settled") or 0):
            continue
        if r.get("competition_status") not in ("open",):
            continue
        label = (
            f"#{r['id']} · {_fmt_plain(int(r['stake']))} on "
            f"{r['outcome_label']} (comp #{r['competition_id']})"
        )
        if q and q not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=int(r["id"])))
        if len(out) >= 25:
            break
    return out


# --- /bouzt balance -------------------------------------------------------

@bouzt_group.command(name="balance", description=f"Show a user's {CURRENCY_NAME} balance.")
@app_commands.describe(user="Defaults to you.")
async def cmd_balance(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
):
    if not await _gate(interaction, write=False):
        return
    target = user or interaction.user
    try:
        bal = service.get_balance(target.id)
    except Exception as e:
        log.exception("balance failed")
        await _reply_error(interaction, f"error: {e}")
        return
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Wallet",
        description=(
            f"{target.mention} has {_fmt(bal)}"
            if target.id != interaction.user.id
            else f"you have {_fmt(bal)}"
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none()
    )


# --- /bouzt leaderboard ---------------------------------------------------

PAGE_SIZE = 10


class LeaderboardView(discord.ui.View):
    def __init__(self, user_id: int, guild: discord.Guild | None, page: int = 0):
        super().__init__(timeout=120)
        self.owner_id = user_id
        self.guild = guild
        self.page = page
        self.total = service.leaderboard_size()
        self._update_buttons()

    def _update_buttons(self):
        max_page = max(0, (self.total - 1) // PAGE_SIZE)
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= max_page

    def _embed(self) -> discord.Embed:
        rows = service.leaderboard(limit=PAGE_SIZE, offset=self.page * PAGE_SIZE)
        max_page = max(0, (self.total - 1) // PAGE_SIZE)
        if not rows:
            desc = "_no wallets yet._"
        else:
            lines = []
            for i, row in enumerate(rows, start=self.page * PAGE_SIZE + 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i:>2}.`")
                name = _user_label(self.guild, int(row["user_id"]))
                lines.append(f"{medal}  **{name}** — {_fmt(int(row['balance']))}")
            desc = "\n".join(lines)
        embed = discord.Embed(
            title=f"{CURRENCY_EMOJI}  {CURRENCY_NAME} Leaderboard",
            description=desc,
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"page {self.page + 1}/{max_page + 1}  •  {self.total} wallets")
        return embed

    async def _refresh(self, interaction: discord.Interaction):
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "this leaderboard isn't yours — run `/bouzt leaderboard` to get your own.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)


@bouzt_group.command(name="leaderboard", description=f"Top {CURRENCY_NAME} holders.")
async def cmd_leaderboard(interaction: discord.Interaction):
    if not await _gate(interaction, write=False):
        return
    view = LeaderboardView(interaction.user.id, interaction.guild, page=0)
    await interaction.response.send_message(
        embed=view._embed(),
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# --- /bouzt give (admin) --------------------------------------------------

@bouzt_group.command(name="give", description=f"(admin) Mint {CURRENCY_NAME} to a user.")
@app_commands.describe(user="Recipient.", amount="Positive integer to mint.")
async def cmd_give(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
):
    if not await _gate(interaction, write=True):
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
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Minted",
        description=(
            f"minted {_fmt(amount)} to {user.mention}\n"
            f"their balance is now {_fmt(new_balance)}"
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none()
    )


# --- /bouzt pay -----------------------------------------------------------

@bouzt_group.command(name="pay", description=f"Transfer {CURRENCY_NAME} to another user.")
@app_commands.describe(user="Who to pay.", amount="Positive integer to send.")
async def cmd_pay(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
):
    if not await _gate(interaction, write=True):
        return
    if user.bot:
        await _reply_error(interaction, "can't pay a bot.")
        return
    try:
        result = service.pay(interaction.user.id, user.id, amount)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("pay failed")
        await _reply_error(interaction, f"error: {e}")
        return
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Payment",
        description=(
            f"{interaction.user.mention} → {user.mention}\n"
            f"sent {_fmt(amount)}"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="balances",
        value=(
            f"{interaction.user.display_name}: {_fmt(result['sender_balance'])}\n"
            f"{user.display_name}: {_fmt(result['recipient_balance'])}"
        ),
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none()
    )


# --- /bouzt bet -----------------------------------------------------------

@bouzt_group.command(name="bet", description="Bet on a competition outcome.")
@app_commands.describe(
    competition_id="Pick from autocomplete.",
    outcome_id="Pick from autocomplete (depends on competition).",
    amount="Stake (positive integer).",
    public="Announce the bet publicly? Defaults to false (ephemeral).",
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
    public: bool = False,
):
    if not await _gate(interaction, write=True):
        return
    try:
        result = await service.place_bet(
            interaction.user.id, competition_id, outcome_id, amount
        )
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("bet failed")
        await _reply_error(interaction, f"error: {e}")
        return
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Bet placed",
        description=(
            f"**{result['outcome_label']}** in #{result['competition_id']} — "
            f"{result['competition_title']}\n"
            f"stake: {_fmt(result['stake'])}  •  balance: {_fmt(result['new_balance'])}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"bet #{result['bet_id']}  •  cancel with /bouzt cancel-bet")
    await interaction.response.send_message(
        embed=embed,
        ephemeral=not public,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# --- /bouzt cancel-bet ----------------------------------------------------

@bouzt_group.command(name="cancel-bet", description="Cancel one of your bets (before the competition locks).")
@app_commands.describe(bet_id="Pick from autocomplete.")
@app_commands.autocomplete(bet_id=_my_open_bet_autocomplete)
async def cmd_cancel_bet(interaction: discord.Interaction, bet_id: int):
    if not await _gate(interaction, write=True):
        return
    try:
        result = await service.cancel_bet(interaction.user.id, bet_id)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("cancel-bet failed")
        await _reply_error(interaction, f"error: {e}")
        return
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Bet cancelled",
        description=(
            f"refunded {_fmt(result['stake'])} from bet #{result['bet_id']}\n"
            f"balance: {_fmt(result['new_balance'])}"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- /bouzt history -------------------------------------------------------

class HistoryView(discord.ui.View):
    def __init__(self, user: discord.abc.User, guild: discord.Guild | None, page: int = 0):
        super().__init__(timeout=120)
        self.user = user
        self.guild = guild
        self.page = page
        self.total = service.bet_history_size(user.id)
        self._update_buttons()

    def _update_buttons(self):
        max_page = max(0, (self.total - 1) // PAGE_SIZE)
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= max_page

    def _embed(self) -> discord.Embed:
        rows = service.bet_history(self.user.id, limit=PAGE_SIZE, offset=self.page * PAGE_SIZE)
        max_page = max(0, (self.total - 1) // PAGE_SIZE)
        if not rows:
            desc = "_no bets yet._"
        else:
            lines = []
            for r in rows:
                settled = int(r.get("settled") or 0)
                result = r.get("result")
                stake = int(r["stake"])
                payout = int(r["payout"] or 0)
                if not settled:
                    icon = "⏳"
                    tail = "pending"
                elif result == "win":
                    icon = "✅"
                    net = payout - stake
                    tail = f"won {_fmt_plain(payout)} (+{_fmt_plain(net)})"
                elif result == "loss":
                    icon = "❌"
                    tail = f"lost {_fmt_plain(stake)}"
                elif result == "refund":
                    icon = "↩️"
                    tail = f"refunded {_fmt_plain(payout)}"
                elif result == "cancelled":
                    icon = "🚫"
                    tail = f"cancelled, refunded {_fmt_plain(payout)}"
                else:
                    icon = "•"
                    tail = "settled"
                lines.append(
                    f"{icon} `#{r['id']}` **{r['outcome_label']}** "
                    f"in #{r['competition_id']} ({r['competition_title']})\n"
                    f"   stake {_fmt_plain(stake)} → {tail}"
                )
            desc = "\n".join(lines)
        embed = discord.Embed(
            title=f"{CURRENCY_EMOJI}  Bet history — {self.user.display_name}",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"page {self.page + 1}/{max_page + 1}  •  {self.total} bets")
        return embed

    async def _refresh(self, interaction: discord.Interaction):
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("not your history.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)


@bouzt_group.command(name="history", description="Your bet history.")
async def cmd_history(interaction: discord.Interaction):
    if not await _gate(interaction, write=False):
        return
    view = HistoryView(interaction.user, interaction.guild, page=0)
    await interaction.response.send_message(embed=view._embed(), view=view, ephemeral=True)


# --- /bouzt help ----------------------------------------------------------

@bouzt_group.command(name="help", description=f"Overview of {CURRENCY_NAME} commands.")
async def cmd_help(interaction: discord.Interaction):
    if not await _gate(interaction, write=False):
        return
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  {CURRENCY_NAME}",
        description=(
            f"every member starts with {_fmt(1000)}. earn more by winning "
            f"pari-mutuel bets on competitions, or send some to a friend."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Wallet",
        value=(
            "`/bouzt balance [user]` — check a balance\n"
            "`/bouzt pay <user> <amount>` — transfer to another user\n"
            "`/bouzt leaderboard` — paginated rich list"
        ),
        inline=False,
    )
    embed.add_field(
        name="Betting",
        value=(
            "`/bouzt competition list` — what's live\n"
            "`/bouzt competition info <id>` — full odds + your bets\n"
            "`/bouzt bet <comp> <outcome> <amount> [public]` — place a bet\n"
            "`/bouzt cancel-bet <bet>` — back out (before lock)\n"
            "`/bouzt history` — your past bets"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin",
        value=(
            "`/bouzt give` · `/bouzt competition create [duration]`\n"
            "`/bouzt competition lock` · `/bouzt competition end` · `/bouzt competition cancel`"
        ),
        inline=False,
    )
    embed.set_footer(text="payout is pari-mutuel: winners split the entire pool, proportional to stake.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- /bouzt competition create -------------------------------------------

@comp_group.command(name="create", description="(admin) Create a new competition.")
@app_commands.describe(
    title="Short title shown in lists.",
    outcomes="Comma-separated outcome labels, e.g. 'Team A, Team B, Draw'.",
    duration="Optional: auto-lock betting after this (e.g. '30m', '2h', '1d').",
)
async def cmd_create(
    interaction: discord.Interaction,
    title: str,
    outcomes: str,
    duration: str | None = None,
):
    if not await _gate(interaction, write=True):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    labels = [s for s in (x.strip() for x in outcomes.split(",")) if s]
    lock_in = None
    if duration:
        try:
            lock_in = service.parse_duration_to_minutes(duration)
        except service.BouztError as e:
            await _reply_error(interaction, str(e))
            return
    try:
        comp = service.create_competition(interaction.user.id, title, labels, lock_in_minutes=lock_in)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("create failed")
        await _reply_error(interaction, f"error: {e}")
        return
    lines = [
        f"`#{int(o['position']) + 1}` · {o['label']}"
        for o in comp["outcomes"]
    ]
    desc_parts = [
        f"bets are open — use `/bouzt bet` and pick from the autocomplete.",
    ]
    if comp.get("locks_at"):
        desc_parts.append(f"⏰ auto-locks {_ts_relative(comp['locks_at'])}")
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Competition #{comp['id']} — {comp['title']}",
        description="\n".join(desc_parts),
        color=discord.Color.green(),
    )
    embed.add_field(name="Outcomes", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)


# --- /bouzt competition list ---------------------------------------------

@comp_group.command(name="list", description="List active competitions (open + locked).")
async def cmd_list(interaction: discord.Interaction):
    if not await _gate(interaction, write=False):
        return
    comps = service.list_active_competitions()
    if not comps:
        embed = discord.Embed(
            description="_no active competitions. ask an admin to start one!_",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.send_message(embed=embed)
        return
    embeds = [_competition_embed(comp) for comp in comps[:10]]
    await interaction.response.send_message(embeds=embeds)


# --- /bouzt competition info ---------------------------------------------

@comp_group.command(name="info", description="Show details of a competition.")
@app_commands.describe(competition_id="Pick from autocomplete.")
@app_commands.autocomplete(competition_id=_any_comp_autocomplete)
async def cmd_info(interaction: discord.Interaction, competition_id: int):
    if not await _gate(interaction, write=False):
        return
    try:
        comp = service.competition_detail(competition_id)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    try:
        my = service.my_bets(interaction.user.id, competition_id)
    except Exception:
        my = []
    await interaction.response.send_message(embed=_competition_embed(comp, my_bets=my))


def _competition_embed(comp: dict, *, my_bets: list | None = None) -> discord.Embed:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    badge, color = _status_badge(comp, now_iso)
    total = int(comp.get("total_pool", 0))
    embed = discord.Embed(
        title=f"#{comp['id']} — {comp['title']}",
        description=f"{badge}  •  pool: {_fmt(total)}",
        color=color,
    )
    rows = []
    for o in comp["outcomes"]:
        pool = int(o.get("pool", 0))
        marker = ""
        if comp.get("status") == "closed" and comp.get("winning_outcome_id") == o["id"]:
            marker = "  🏆"
        share = (pool / total * 100) if total > 0 else 0
        rows.append(
            f"`#{int(o['position']) + 1}` **{o['label']}**{marker}\n"
            f"    {_odds(pool, total)}  ·  {_fmt_plain(pool)}  ·  {share:.0f}%"
        )
    embed.add_field(name="Outcomes", value="\n".join(rows) or "(none)", inline=False)
    if my_bets:
        lines = []
        for b in my_bets:
            settled = int(b.get("settled") or 0)
            tag = ""
            if settled:
                res = b.get("result")
                if res == "win":
                    tag = f" → won {_fmt_plain(int(b['payout']))}"
                elif res == "loss":
                    tag = " → lost"
                elif res == "refund":
                    tag = f" → refunded {_fmt_plain(int(b['payout']))}"
                elif res == "cancelled":
                    tag = " → cancelled"
            lines.append(f"• `#{b['id']}` {_fmt_plain(int(b['stake']))} on **{b['outcome_label']}**{tag}")
        embed.add_field(name="Your bets", value="\n".join(lines), inline=False)
    if comp.get("status") == "open" and comp.get("locks_at"):
        embed.set_footer(text="⏰ tip: betting auto-locks at the timer above.")
    return embed


# --- /bouzt competition lock ---------------------------------------------

@comp_group.command(name="lock", description="(admin) Lock betting on a competition (no more bets).")
@app_commands.describe(competition_id="Pick from autocomplete.")
@app_commands.autocomplete(competition_id=_open_comp_autocomplete)
async def cmd_lock(interaction: discord.Interaction, competition_id: int):
    if not await _gate(interaction, write=True):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    try:
        result = await service.lock_competition(interaction.user.id, competition_id)
    except service.BouztError as e:
        await _reply_error(interaction, str(e))
        return
    except Exception as e:
        log.exception("lock failed")
        await _reply_error(interaction, f"error: {e}")
        return
    if result.get("already"):
        await _reply_error(interaction, "already locked.")
        return
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI}  Betting locked",
        description=(
            f"**#{result['competition_id']} — {result['title']}**\n"
            f"no more bets. waiting on the result."
        ),
        color=discord.Color.dark_orange(),
    )
    await interaction.response.send_message(embed=embed)


# --- /bouzt competition end ----------------------------------------------

@comp_group.command(name="end", description="(admin) End a competition by declaring the winner.")
@app_commands.describe(
    competition_id="ID of the competition to settle.",
    winning_outcome_id="The winning outcome ID.",
)
@app_commands.autocomplete(
    competition_id=_active_comp_autocomplete,
    winning_outcome_id=_outcomes_for_comp_autocomplete,
)
async def cmd_end(
    interaction: discord.Interaction,
    competition_id: int,
    winning_outcome_id: int,
):
    if not await _gate(interaction, write=True):
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
        await interaction.followup.send(f"⚠️  {e}", ephemeral=True)
        return
    except Exception as e:
        log.exception("end failed")
        await interaction.followup.send(f"⚠️  error: {e}", ephemeral=True)
        return

    color = discord.Color.orange() if result["refunded"] else discord.Color.gold()
    title = f"🏆  Competition #{result['competition_id']} — {result['title']}"
    if result["refunded"]:
        desc = (
            f"winner: **{result['winning_outcome_label']}** _(no one bet on it)_\n"
            f"refunded {result['bet_count']} bets totalling {_fmt(result['total_pool'])}."
        )
    elif result["bet_count"] == 0:
        desc = "no bets were placed. closed with no payouts."
    else:
        lines = [
            f"winner: **{result['winning_outcome_label']}**",
            (
                f"pool {_fmt(result['total_pool'])}  •  "
                f"winners' stake {_fmt(result['winners_pool'])}  •  "
                f"losers' stake {_fmt(result['losers_pool'])}"
            ),
            f"{result['winner_count']}/{result['bet_count']} bets won",
        ]
        if result["remainder"]:
            lines.append(f"_unallocated remainder: {_fmt_plain(result['remainder'])}_")
        desc = "\n".join(lines)
    embed = discord.Embed(title=title, description=desc, color=color)
    if not result["refunded"] and result["bet_count"] > 0:
        winners = [p for p in result["payouts"] if p["payout"] > 0]
        if winners:
            lines = []
            for p in winners[:25]:
                name = _user_label(interaction.guild, int(p["user_id"]))
                net = p["payout"] - p["stake"]
                lines.append(
                    f"🥇  **{name}** — staked {_fmt_plain(p['stake'])} → "
                    f"**{_fmt_plain(p['payout'])}** (+{_fmt_plain(net)})"
                )
            embed.add_field(name="Payouts", value="\n".join(lines), inline=False)
    await interaction.followup.send(embed=embed)


# --- /bouzt competition cancel -------------------------------------------

@comp_group.command(name="cancel", description="(admin) Cancel a competition and refund every bet.")
@app_commands.describe(competition_id="ID of the competition to cancel.")
@app_commands.autocomplete(competition_id=_active_comp_autocomplete)
async def cmd_cancel(interaction: discord.Interaction, competition_id: int):
    if not await _gate(interaction, write=True):
        return
    if not _is_admin(interaction.user.id):
        await _reply_error(interaction, "admin only.")
        return
    await interaction.response.defer()
    try:
        result = await service.cancel_competition(interaction.user.id, competition_id)
    except service.BouztError as e:
        await interaction.followup.send(f"⚠️  {e}", ephemeral=True)
        return
    except Exception as e:
        log.exception("cancel failed")
        await interaction.followup.send(f"⚠️  error: {e}", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"✖️  Competition #{result['competition_id']} cancelled",
        description=(
            f"refunded {result['bet_count']} bets totalling "
            f"{_fmt(result['refunded_total'])}."
        ),
        color=discord.Color.red(),
    )
    await interaction.followup.send(embed=embed)
