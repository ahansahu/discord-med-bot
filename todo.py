"""Pinned todo list — multi-section list controlled by slash commands and a
persistent button + modal UI on the pinned message."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

from config import TARGET_USER_ID, TODO_CHANNEL_ID
import storage

log = logging.getLogger("med_bot.todo")

PAGE_SIZE = 20
SECTIONS_PER_PAGE = 4
ITEM_LABEL_MAX = 80
EMPTY_PLACEHOLDER = "_(empty)_"
DEFAULT_SECTION = "Buy"
PINNED_STATE_KEY = "pinned_msg_id"

_ui_state = {"page": 0, "mode": "normal", "selected_item_id": None}


# --- helpers --------------------------------------------------------------

def _truncate(text: str) -> str:
    return text if len(text) <= ITEM_LABEL_MAX else text[: ITEM_LABEL_MAX - 1] + "…"


def _is_target(interaction: discord.Interaction) -> bool:
    return interaction.user.id == TARGET_USER_ID


async def _gate(interaction: discord.Interaction) -> bool:
    """Returns True if the interaction should be ignored (and a reply sent)."""
    if not _is_target(interaction):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True
        )
        return True
    return False


def _build_sections_with_items() -> tuple[list[dict], list[dict]]:
    sections = storage.todo_sections_ordered()
    flat: list[dict] = []
    for sec in sections:
        sec_items = storage.todo_items_for_section(sec["id"])
        sec["items"] = sec_items
        for it in sec_items:
            it["section_name"] = sec["name"]
        flat.extend(sec_items)
    return sections, flat


def _resolve_section(name: str) -> int:
    name = (name or "").strip()
    if name:
        sec = storage.todo_section_by_name(name)
        if sec is None:
            return storage.todo_section_create(name)
        return sec["id"]
    sections = storage.todo_sections_ordered()
    if sections:
        return sections[0]["id"]
    return storage.todo_section_create(DEFAULT_SECTION)


def _total_pages(items: list[dict]) -> int:
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def _clamp_state(items: list[dict]) -> None:
    pages = _total_pages(items)
    if _ui_state["page"] >= pages:
        _ui_state["page"] = pages - 1
    if _ui_state["page"] < 0:
        _ui_state["page"] = 0
    sel = _ui_state["selected_item_id"]
    if sel is not None and not any(it["id"] == sel for it in items):
        _ui_state["selected_item_id"] = None


def render_text(sections: list[dict], page: int, total_pages: int) -> str:
    blocks: list[str] = []
    for sec in sections:
        items = sec.get("items", [])
        if not items:
            continue
        body = "\n".join(it["text"] for it in items)
        blocks.append(f"{sec['name']}-\n\n{body}")
    text = "\n\n".join(blocks) if blocks else EMPTY_PLACEHOLDER
    if total_pages > 1:
        text += f"\n\n_Page {page + 1}/{total_pages}_"
    return text


# --- pinned message lifecycle ---------------------------------------------

async def _resolve_channel(bot: commands.Bot) -> Optional[discord.abc.Messageable]:
    if TODO_CHANNEL_ID is None:
        return None
    channel = bot.get_channel(TODO_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(TODO_CHANNEL_ID)
        except discord.HTTPException as e:
            log.warning("could not fetch todo channel: %s", e)
            return None
    return channel


async def ensure_pinned(bot: commands.Bot) -> Optional[discord.Message]:
    """Ensure a pinned todo message exists; create + pin if missing."""
    channel = await _resolve_channel(bot)
    if channel is None:
        return None
    msg_id_str = storage.todo_get_state(PINNED_STATE_KEY)
    msg = None
    if msg_id_str:
        try:
            msg = await channel.fetch_message(int(msg_id_str))
        except discord.NotFound:
            msg = None
        except discord.HTTPException as e:
            log.warning("ensure_pinned: fetch failed: %s", e)
            return None
    sections, items = _build_sections_with_items()
    _clamp_state(items)
    view = TodoView(items)
    content = render_text(sections, _ui_state["page"], _total_pages(items))
    if msg is None:
        msg = await channel.send(content=content, view=view)
        try:
            await msg.pin()
        except discord.HTTPException as e:
            log.warning("could not pin todo message: %s", e)
        storage.todo_set_state(PINNED_STATE_KEY, str(msg.id))
    else:
        await msg.edit(content=content, view=view)
    return msg


async def rerender(bot: commands.Bot) -> None:
    """Fetch the pinned message and edit it with fresh state."""
    channel = await _resolve_channel(bot)
    if channel is None:
        return
    msg_id_str = storage.todo_get_state(PINNED_STATE_KEY)
    if not msg_id_str:
        await ensure_pinned(bot)
        return
    try:
        msg = await channel.fetch_message(int(msg_id_str))
    except discord.NotFound:
        log.info("pinned todo message missing; recreating")
        await ensure_pinned(bot)
        return
    except discord.HTTPException as e:
        log.warning("rerender fetch failed: %s", e)
        return
    sections, items = _build_sections_with_items()
    _clamp_state(items)
    text = render_text(sections, _ui_state["page"], _total_pages(items))
    view = TodoView(items)
    await msg.edit(content=text, view=view)


async def _ack_inline(interaction: discord.Interaction) -> None:
    """Edit the message the component is on with fresh todo state."""
    sections, items = _build_sections_with_items()
    _clamp_state(items)
    text = render_text(sections, _ui_state["page"], _total_pages(items))
    view = TodoView(items)
    await interaction.response.edit_message(content=text, view=view)


# --- main pinned view -----------------------------------------------------

class TodoView(ui.View):
    def __init__(self, items_flat: list[dict]):
        super().__init__(timeout=None)
        _clamp_state(items_flat)
        page = _ui_state["page"]
        mode = _ui_state["mode"]
        selected = _ui_state["selected_item_id"]
        total = _total_pages(items_flat)
        start = page * PAGE_SIZE
        on_page = items_flat[start : start + PAGE_SIZE]
        num_item_rows = (len(on_page) + 4) // 5
        ctrl_row = min(4, num_item_rows)
        for idx, it in enumerate(on_page):
            row = idx // 5
            is_selected = mode == "edit" and selected == it["id"]
            if is_selected:
                style = discord.ButtonStyle.success
            elif mode == "edit":
                style = discord.ButtonStyle.primary
            else:
                style = discord.ButtonStyle.secondary
            self.add_item(TodoItemButton(it["id"], _truncate(it["text"]), style, row))
        if mode == "edit":
            self.add_item(ActionButton("top", "⬆ Top", "todo:act:top", ctrl_row))
            self.add_item(ActionButton("up", "▲ Up", "todo:act:up", ctrl_row))
            self.add_item(ActionButton("down", "▼ Down", "todo:act:down", ctrl_row))
            self.add_item(ActionButton("bottom", "⬇ Bottom", "todo:act:bottom", ctrl_row))
            self.add_item(ExitEditButton(ctrl_row))
        else:
            self.add_item(AddButton(ctrl_row))
            self.add_item(SectionsButton(ctrl_row))
            self.add_item(EditButton(ctrl_row))
            self.add_item(PagePrevButton(ctrl_row, disabled=page <= 0))
            self.add_item(PageNextButton(ctrl_row, disabled=page >= total - 1))


class TodoItemButton(ui.Button):
    def __init__(self, item_id: int, label: str, style: discord.ButtonStyle, row: int):
        super().__init__(
            style=style,
            label=label,
            custom_id=f"todo:item:{item_id}",
            row=row,
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        if _ui_state["mode"] == "edit":
            _ui_state["selected_item_id"] = self.item_id
        else:
            storage.todo_item_delete(self.item_id)
        await _ack_inline(interaction)


class AddButton(ui.Button):
    def __init__(self, row: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="➕ Add",
            custom_id="todo:add",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        await interaction.response.send_modal(AddItemModal())


class SectionsButton(ui.Button):
    def __init__(self, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="🗂 Sections",
            custom_id="todo:sections",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        sections = storage.todo_sections_ordered()
        await interaction.response.send_message(
            content="**Sections**",
            view=SectionsView(sections, page=0),
            ephemeral=True,
        )


class EditButton(ui.Button):
    def __init__(self, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↕ Edit",
            custom_id="todo:edit",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        _ui_state["mode"] = "edit"
        _ui_state["selected_item_id"] = None
        await _ack_inline(interaction)


class PagePrevButton(ui.Button):
    def __init__(self, row: int, disabled: bool):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="‹",
            custom_id="todo:page:prev",
            row=row,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        if _ui_state["page"] > 0:
            _ui_state["page"] -= 1
        await _ack_inline(interaction)


class PageNextButton(ui.Button):
    def __init__(self, row: int, disabled: bool):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="›",
            custom_id="todo:page:next",
            row=row,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        _ui_state["page"] += 1
        await _ack_inline(interaction)


class ActionButton(ui.Button):
    def __init__(self, action: str, label: str, custom_id: str, row: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=custom_id,
            row=row,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        sel = _ui_state["selected_item_id"]
        if sel is None:
            await interaction.response.send_message(
                "pick an item first.", ephemeral=True
            )
            return
        if self.action == "up":
            storage.todo_item_move(sel, "up")
        elif self.action == "down":
            storage.todo_item_move(sel, "down")
        elif self.action == "top":
            storage.todo_item_priority(sel, "high")
        elif self.action == "bottom":
            storage.todo_item_priority(sel, "low")
        await _ack_inline(interaction)


class ExitEditButton(ui.Button):
    def __init__(self, row: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="✕ Exit",
            custom_id="todo:act:cancel",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        _ui_state["mode"] = "normal"
        _ui_state["selected_item_id"] = None
        await _ack_inline(interaction)


# --- modals ---------------------------------------------------------------

class AddItemModal(ui.Modal, title="Add todo"):
    item_text: ui.TextInput = ui.TextInput(
        label="Item", required=True, max_length=200, placeholder="Treats for Mu"
    )
    section_name: ui.TextInput = ui.TextInput(
        label="Section (optional)",
        required=False,
        max_length=50,
        placeholder="leave blank to use the first section",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.item_text.value).strip()
        if not text:
            await interaction.response.send_message(
                "item text cannot be empty.", ephemeral=True
            )
            return
        section_id = _resolve_section(str(self.section_name.value or ""))
        storage.todo_item_add(section_id, text)
        await _ack_inline(interaction)


class NewSectionModal(ui.Modal, title="New section"):
    name: ui.TextInput = ui.TextInput(
        label="Name", required=True, max_length=50, placeholder="Errands"
    )

    def __init__(self, parent_interaction: discord.Interaction):
        super().__init__()
        self.parent_interaction = parent_interaction

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.name.value).strip()
        if not name:
            await interaction.response.send_message(
                "name cannot be empty.", ephemeral=True
            )
            return
        if storage.todo_section_by_name(name):
            await interaction.response.send_message(
                f"section `{name}` already exists.", ephemeral=True
            )
            return
        storage.todo_section_create(name)
        await interaction.response.defer()
        sections = storage.todo_sections_ordered()
        await self.parent_interaction.edit_original_response(
            view=SectionsView(sections, page=0)
        )
        await rerender(interaction.client)


class RenameSectionModal(ui.Modal, title="Rename section"):
    new_name: ui.TextInput = ui.TextInput(
        label="New name", required=True, max_length=50
    )

    def __init__(
        self,
        section_id: int,
        current_name: str,
        parent_interaction: discord.Interaction,
        page: int,
    ):
        super().__init__()
        self.section_id = section_id
        self.parent_interaction = parent_interaction
        self.page = page
        self.new_name.default = current_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new = str(self.new_name.value).strip()
        if not new:
            await interaction.response.send_message(
                "name cannot be empty.", ephemeral=True
            )
            return
        existing = storage.todo_section_by_name(new)
        if existing and existing["id"] != self.section_id:
            await interaction.response.send_message(
                f"section `{new}` already exists.", ephemeral=True
            )
            return
        storage.todo_section_rename(self.section_id, new)
        await interaction.response.defer()
        sections = storage.todo_sections_ordered()
        await self.parent_interaction.edit_original_response(
            view=SectionsView(sections, page=self.page)
        )
        await rerender(interaction.client)


# --- sections submenu (ephemeral) -----------------------------------------

class SectionsView(ui.View):
    def __init__(self, sections: list[dict], page: int = 0):
        super().__init__(timeout=300)
        self.sections = sections
        total_pages = max(
            1, (len(sections) + SECTIONS_PER_PAGE - 1) // SECTIONS_PER_PAGE
        )
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        self.page = page
        start = page * SECTIONS_PER_PAGE
        page_secs = sections[start : start + SECTIONS_PER_PAGE]
        for idx, sec in enumerate(page_secs):
            row = idx
            self.add_item(
                ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=_truncate(sec["name"]),
                    disabled=True,
                    row=row,
                )
            )
            self.add_item(SectionRenameButton(sec["id"], sec["name"], page, row))
            self.add_item(SectionMoveButton(sec["id"], "up", page, row))
            self.add_item(SectionMoveButton(sec["id"], "down", page, row))
            self.add_item(SectionDeleteButton(sec["id"], sec["name"], page, row))
        ctrl_row = min(4, SECTIONS_PER_PAGE)
        self.add_item(NewSectionButton(ctrl_row))
        if total_pages > 1:
            self.add_item(SectionsPagePrevButton(page, ctrl_row, disabled=page <= 0))
            self.add_item(
                SectionsPageNextButton(page, ctrl_row, disabled=page >= total_pages - 1)
            )
        self.add_item(CloseSectionsButton(ctrl_row))


async def _refresh_sections(interaction: discord.Interaction, page: int) -> None:
    sections = storage.todo_sections_ordered()
    await interaction.response.edit_message(view=SectionsView(sections, page=page))


class SectionRenameButton(ui.Button):
    def __init__(self, section_id: int, name: str, page: int, row: int):
        super().__init__(
            style=discord.ButtonStyle.primary, label="✎ Rename", row=row
        )
        self.section_id = section_id
        self.name = name
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        await interaction.response.send_modal(
            RenameSectionModal(self.section_id, self.name, interaction, self.page)
        )


class SectionMoveButton(ui.Button):
    def __init__(self, section_id: int, direction: str, page: int, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="▲" if direction == "up" else "▼",
            row=row,
        )
        self.section_id = section_id
        self.direction = direction
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        storage.todo_section_move(self.section_id, self.direction)
        await _refresh_sections(interaction, self.page)
        await rerender(interaction.client)


class SectionDeleteButton(ui.Button):
    def __init__(self, section_id: int, name: str, page: int, row: int):
        super().__init__(style=discord.ButtonStyle.danger, label="🗑", row=row)
        self.section_id = section_id
        self.name = name
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        removed = storage.todo_section_delete(self.section_id)
        await _refresh_sections(interaction, self.page)
        await interaction.followup.send(
            f"🗑 removed section `{self.name}` ({removed} "
            f"{'item' if removed == 1 else 'items'} cleared).",
            ephemeral=True,
        )
        await rerender(interaction.client)


class NewSectionButton(ui.Button):
    def __init__(self, row: int):
        super().__init__(
            style=discord.ButtonStyle.success, label="➕ New section", row=row
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        await interaction.response.send_modal(NewSectionModal(interaction))


class SectionsPagePrevButton(ui.Button):
    def __init__(self, page: int, row: int, disabled: bool):
        super().__init__(
            style=discord.ButtonStyle.secondary, label="‹", row=row, disabled=disabled
        )
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        await _refresh_sections(interaction, max(0, self.page - 1))


class SectionsPageNextButton(ui.Button):
    def __init__(self, page: int, row: int, disabled: bool):
        super().__init__(
            style=discord.ButtonStyle.secondary, label="›", row=row, disabled=disabled
        )
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _gate(interaction):
            return
        await _refresh_sections(interaction, self.page + 1)


class CloseSectionsButton(ui.Button):
    def __init__(self, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary, label="Close", row=row
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="_(closed)_", view=None
        )


# --- slash commands -------------------------------------------------------

todo_group = app_commands.Group(name="todo", description="Manage the todo list.")


async def _wrong_todo_channel(interaction: discord.Interaction) -> bool:
    if TODO_CHANNEL_ID is None:
        await interaction.response.send_message(
            "todo channel isn't configured (set `TODO_CHANNEL_ID`).",
            ephemeral=True,
        )
        return True
    if interaction.channel_id != TODO_CHANNEL_ID:
        await interaction.response.send_message(
            f"please use this command in <#{TODO_CHANNEL_ID}> 🙏",
            ephemeral=True,
        )
        return True
    return False


async def _slash_gate(interaction: discord.Interaction) -> bool:
    """Channel + target-user guard for /todo commands. True → reject."""
    if await _wrong_todo_channel(interaction):
        return True
    if not _is_target(interaction):
        await interaction.response.send_message(
            "this bot only tracks one user 🙏", ephemeral=True
        )
        return True
    return False


async def _item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    items = storage.todo_items_all_ordered()
    needle = current.lower()
    out: list[app_commands.Choice[str]] = []
    for it in items:
        if needle and needle not in it["text"].lower():
            continue
        label = f"{it['text']} ({it['section_name']})"
        out.append(app_commands.Choice(name=label[:100], value=str(it["id"])))
        if len(out) >= 25:
            break
    return out


async def _section_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    sections = storage.todo_sections_ordered()
    needle = current.lower()
    out: list[app_commands.Choice[str]] = []
    for s in sections:
        if needle and needle not in s["name"].lower():
            continue
        out.append(app_commands.Choice(name=s["name"][:100], value=s["name"]))
        if len(out) >= 25:
            break
    return out


def _resolve_item_choice(value: str) -> Optional[dict]:
    try:
        item_id = int(value)
    except ValueError:
        return None
    return storage.todo_item_get(item_id)


@todo_group.command(name="add", description="Add an item to the todo list.")
@app_commands.describe(
    item="Item text",
    section="Section name (optional). Created if it doesn't exist.",
)
@app_commands.autocomplete(section=_section_autocomplete)
async def cmd_todo_add(
    interaction: discord.Interaction,
    item: str,
    section: Optional[str] = None,
) -> None:
    if await _slash_gate(interaction):
        return
    text = item.strip()
    if not text:
        await interaction.response.send_message(
            "item text cannot be empty.", ephemeral=True
        )
        return
    section_id = _resolve_section(section or "")
    storage.todo_item_add(section_id, text)
    await interaction.response.send_message(
        f"✅ added `{text}`.", ephemeral=True
    )
    await rerender(interaction.client)


@todo_group.command(name="done", description="Mark a todo item as done (removes it).")
@app_commands.describe(item="Pick an item from the autocomplete.")
@app_commands.autocomplete(item=_item_autocomplete)
async def cmd_todo_done(interaction: discord.Interaction, item: str) -> None:
    if await _slash_gate(interaction):
        return
    found = _resolve_item_choice(item)
    if found is None:
        await interaction.response.send_message(
            "use the autocomplete to pick an item.", ephemeral=True
        )
        return
    storage.todo_item_delete(found["id"])
    await interaction.response.send_message(
        f"✅ done: `{found['text']}`.", ephemeral=True
    )
    await rerender(interaction.client)


@todo_group.command(name="move", description="Move an item up or down within its section.")
@app_commands.describe(item="Pick an item from the autocomplete.", direction="up or down")
@app_commands.autocomplete(item=_item_autocomplete)
@app_commands.choices(
    direction=[
        app_commands.Choice(name="up", value="up"),
        app_commands.Choice(name="down", value="down"),
    ]
)
async def cmd_todo_move(
    interaction: discord.Interaction,
    item: str,
    direction: app_commands.Choice[str],
) -> None:
    if await _slash_gate(interaction):
        return
    found = _resolve_item_choice(item)
    if found is None:
        await interaction.response.send_message(
            "use the autocomplete to pick an item.", ephemeral=True
        )
        return
    moved = storage.todo_item_move(found["id"], direction.value)
    if not moved:
        await interaction.response.send_message(
            f"`{found['text']}` is already at the {'top' if direction.value == 'up' else 'bottom'}.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"↕ moved `{found['text']}` {direction.value}.", ephemeral=True
    )
    await rerender(interaction.client)


@todo_group.command(name="priority", description="Jump an item to the top or bottom of its section.")
@app_commands.describe(item="Pick an item.", level="high (top) or low (bottom)")
@app_commands.autocomplete(item=_item_autocomplete)
@app_commands.choices(
    level=[
        app_commands.Choice(name="high", value="high"),
        app_commands.Choice(name="low", value="low"),
    ]
)
async def cmd_todo_priority(
    interaction: discord.Interaction,
    item: str,
    level: app_commands.Choice[str],
) -> None:
    if await _slash_gate(interaction):
        return
    found = _resolve_item_choice(item)
    if found is None:
        await interaction.response.send_message(
            "use the autocomplete to pick an item.", ephemeral=True
        )
        return
    storage.todo_item_priority(found["id"], level.value)
    await interaction.response.send_message(
        f"↕ {'pinned to top' if level.value == 'high' else 'sent to bottom'}: `{found['text']}`.",
        ephemeral=True,
    )
    await rerender(interaction.client)


section_group = app_commands.Group(
    name="section", description="Manage todo sections.", parent=todo_group
)


@section_group.command(name="add", description="Create a new section.")
@app_commands.describe(name="Section name")
async def cmd_section_add(interaction: discord.Interaction, name: str) -> None:
    if await _slash_gate(interaction):
        return
    name = name.strip()
    if not name:
        await interaction.response.send_message(
            "name cannot be empty.", ephemeral=True
        )
        return
    if storage.todo_section_by_name(name):
        await interaction.response.send_message(
            f"section `{name}` already exists.", ephemeral=True
        )
        return
    storage.todo_section_create(name)
    await interaction.response.send_message(
        f"✅ created section `{name}`.", ephemeral=True
    )
    await rerender(interaction.client)


@section_group.command(name="remove", description="Delete a section and its items.")
@app_commands.describe(name="Section name")
@app_commands.autocomplete(name=_section_autocomplete)
async def cmd_section_remove(interaction: discord.Interaction, name: str) -> None:
    if await _slash_gate(interaction):
        return
    sec = storage.todo_section_by_name(name.strip())
    if sec is None:
        await interaction.response.send_message(
            f"section `{name}` not found.", ephemeral=True
        )
        return
    removed = storage.todo_section_delete(sec["id"])
    await interaction.response.send_message(
        f"🗑 removed section `{name}` ({removed} "
        f"{'item' if removed == 1 else 'items'} cleared).",
        ephemeral=True,
    )
    await rerender(interaction.client)


@section_group.command(name="rename", description="Rename a section.")
@app_commands.describe(old="Current name", new="New name")
@app_commands.autocomplete(old=_section_autocomplete)
async def cmd_section_rename(
    interaction: discord.Interaction, old: str, new: str
) -> None:
    if await _slash_gate(interaction):
        return
    new = new.strip()
    if not new:
        await interaction.response.send_message(
            "new name cannot be empty.", ephemeral=True
        )
        return
    sec = storage.todo_section_by_name(old.strip())
    if sec is None:
        await interaction.response.send_message(
            f"section `{old}` not found.", ephemeral=True
        )
        return
    existing = storage.todo_section_by_name(new)
    if existing and existing["id"] != sec["id"]:
        await interaction.response.send_message(
            f"section `{new}` already exists.", ephemeral=True
        )
        return
    storage.todo_section_rename(sec["id"], new)
    await interaction.response.send_message(
        f"✏️ renamed `{old}` → `{new}`.", ephemeral=True
    )
    await rerender(interaction.client)


@section_group.command(name="move", description="Move a section up or down.")
@app_commands.describe(name="Section name", direction="up or down")
@app_commands.autocomplete(name=_section_autocomplete)
@app_commands.choices(
    direction=[
        app_commands.Choice(name="up", value="up"),
        app_commands.Choice(name="down", value="down"),
    ]
)
async def cmd_section_move(
    interaction: discord.Interaction,
    name: str,
    direction: app_commands.Choice[str],
) -> None:
    if await _slash_gate(interaction):
        return
    sec = storage.todo_section_by_name(name.strip())
    if sec is None:
        await interaction.response.send_message(
            f"section `{name}` not found.", ephemeral=True
        )
        return
    moved = storage.todo_section_move(sec["id"], direction.value)
    if not moved:
        await interaction.response.send_message(
            f"`{name}` is already at the {'top' if direction.value == 'up' else 'bottom'}.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"↕ moved `{name}` {direction.value}.", ephemeral=True
    )
    await rerender(interaction.client)


def register(bot: commands.Bot) -> None:
    """Register the /todo command tree with the bot. Idempotent."""
    if TODO_CHANNEL_ID is None:
        return
    if bot.tree.get_command("todo") is None:
        bot.tree.add_command(todo_group)
