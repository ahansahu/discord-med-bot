import calendar
import io
import logging
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from config import STICKER_DIR, TZ
import storage


log = logging.getLogger("med_bot.chart")

STICKER_SIZE = 96
STICKER_COUNT = 6
CUSTOM_PREFIX = "custom_"

ASSETS_DIR = Path(__file__).parent / "assets"
DECOR_DIR = ASSETS_DIR / "decor"
FONT_DIR = ASSETS_DIR / "fonts"
BG_PATH = DECOR_DIR / "background.png"
BG_PATH_WEEK = DECOR_DIR / "background_week.png"
SERIF_FONT_CANDIDATES = [
    (FONT_DIR / "Fraunces-VF.ttf", "SemiBold"),
    (FONT_DIR / "PlayfairDisplay-VF.ttf", "Bold"),
]
EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguiemj.ttf",
]
# Noto Color Emoji ships a single CBDT strike at 109px; Pillow can scale it
# down only when loaded at that native size.
EMOJI_FONT_NATIVE_SIZE = 109


# ---------- font loading ----------

def _load_font(size: int, prefer_serif: bool = False) -> ImageFont.ImageFont:
    if prefer_serif:
        for path, instance in SERIF_FONT_CANDIDATES:
            if not path.exists():
                continue
            try:
                font = ImageFont.truetype(str(path), size)
                try:
                    font.set_variation_by_name(instance)
                except (OSError, AttributeError):
                    pass
                return font
            except (OSError, IOError):
                continue
    candidates = [
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _load_emoji_font() -> Optional[ImageFont.ImageFont]:
    for path in EMOJI_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, EMOJI_FONT_NATIVE_SIZE)
        except (OSError, IOError):
            continue
    return None


# ---------- procedural sticker drawing ----------

def _draw_star(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
               fill: tuple, outline: tuple, points: int = 5) -> None:
    pts = []
    for i in range(points * 2):
        angle = -math.pi / 2 + i * math.pi / points
        radius = r if i % 2 == 0 else r * 0.45
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    draw.polygon(pts, fill=fill, outline=outline)


def _draw_heart(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
                fill: tuple, outline: tuple) -> None:
    # two circles + triangle
    lobe_r = int(r * 0.55)
    left_c = (cx - lobe_r // 2 - 2, cy - lobe_r // 3)
    right_c = (cx + lobe_r // 2 + 2, cy - lobe_r // 3)
    draw.ellipse(
        [left_c[0] - lobe_r, left_c[1] - lobe_r, left_c[0] + lobe_r, left_c[1] + lobe_r],
        fill=fill, outline=outline, width=2,
    )
    draw.ellipse(
        [right_c[0] - lobe_r, right_c[1] - lobe_r, right_c[0] + lobe_r, right_c[1] + lobe_r],
        fill=fill, outline=outline, width=2,
    )
    # bottom triangle
    tip = (cx, cy + r)
    left = (cx - r + 2, cy - lobe_r // 4 + 2)
    right = (cx + r - 2, cy - lobe_r // 4 + 2)
    draw.polygon([left, right, tip], fill=fill, outline=outline)
    # cover the seam
    draw.polygon([left, right, tip], fill=fill)


def _draw_smiley(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 215, 64), outline=(200, 150, 0), width=3)
    eye_r = max(3, r // 8)
    draw.ellipse([cx - r // 3 - eye_r, cy - r // 4 - eye_r,
                  cx - r // 3 + eye_r, cy - r // 4 + eye_r], fill=(0, 0, 0))
    draw.ellipse([cx + r // 3 - eye_r, cy - r // 4 - eye_r,
                  cx + r // 3 + eye_r, cy - r // 4 + eye_r], fill=(0, 0, 0))
    # smile
    draw.arc([cx - r // 2, cy - r // 4, cx + r // 2, cy + r // 2],
             start=0, end=180, fill=(0, 0, 0), width=4)


def _draw_sun(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    # rays
    rays = 12
    inner = int(r * 0.7)
    outer = r
    for i in range(rays):
        a = i * 2 * math.pi / rays
        x1 = cx + inner * math.cos(a)
        y1 = cy + inner * math.sin(a)
        x2 = cx + outer * math.cos(a)
        y2 = cy + outer * math.sin(a)
        draw.line([(x1, y1), (x2, y2)], fill=(255, 152, 0), width=5)
    body = int(r * 0.65)
    draw.ellipse([cx - body, cy - body, cx + body, cy + body],
                 fill=(255, 213, 79), outline=(245, 124, 0), width=3)


def _draw_sparkle(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    # 4-point star
    pts = [
        (cx, cy - r),
        (cx + r * 0.25, cy - r * 0.25),
        (cx + r, cy),
        (cx + r * 0.25, cy + r * 0.25),
        (cx, cy + r),
        (cx - r * 0.25, cy + r * 0.25),
        (cx - r, cy),
        (cx - r * 0.25, cy - r * 0.25),
    ]
    draw.polygon(pts, fill=(236, 64, 122), outline=(173, 20, 87))
    # tiny dots
    for dx, dy in [(int(r * 0.7), -int(r * 0.6)), (-int(r * 0.7), int(r * 0.6))]:
        dr = max(2, r // 12)
        draw.ellipse([cx + dx - dr, cy + dy - dr, cx + dx + dr, cy + dy + dr],
                     fill=(244, 143, 177))


def _draw_flower(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    petals = 6
    petal_r = int(r * 0.42)
    pr = int(r * 0.55)
    for i in range(petals):
        a = i * 2 * math.pi / petals
        px = cx + pr * math.cos(a)
        py = cy + pr * math.sin(a)
        draw.ellipse([px - petal_r, py - petal_r, px + petal_r, py + petal_r],
                     fill=(186, 104, 200), outline=(106, 27, 154), width=2)
    cr = int(r * 0.32)
    draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr],
                 fill=(255, 235, 59), outline=(245, 127, 23), width=2)


_STICKER_DRAWERS = [
    lambda d, cx, cy, r: _draw_star(d, cx, cy, r, (255, 235, 59), (245, 127, 23)),
    lambda d, cx, cy, r: _draw_heart(d, cx, cy, r, (244, 67, 54), (183, 28, 28)),
    _draw_smiley,
    _draw_sun,
    _draw_sparkle,
    _draw_flower,
]


def _ensure_stickers() -> list[Path]:
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, drawer in enumerate(_STICKER_DRAWERS):
        p = STICKER_DIR / f"sticker_{i}.png"
        if not p.exists():
            img = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            drawer(d, STICKER_SIZE // 2, STICKER_SIZE // 2, STICKER_SIZE // 2 - 6)
            img.save(p, "PNG")
        paths.append(p)
    return paths


def _sticker_pool() -> list[Path]:
    builtin = _ensure_stickers()
    customs = sorted(
        p for p in STICKER_DIR.glob(f"{CUSTOM_PREFIX}*.png") if p.is_file()
    )
    return builtin + customs


def _legend_taken_sticker_index(pool: list[Path]) -> int:
    # Prefer a custom sticker so the legend matches what the user actually
    # sees on the chart when they've uploaded their own stickers.
    if len(pool) > STICKER_COUNT:
        return STICKER_COUNT
    return 1


def random_sticker_index() -> int:
    # Pool order is built-ins first (indices 0..STICKER_COUNT-1), then customs.
    # Prefer customs when any are present; fall back to built-ins otherwise.
    pool = _sticker_pool()
    if len(pool) > STICKER_COUNT:
        return random.randrange(STICKER_COUNT, len(pool))
    return random.randrange(STICKER_COUNT)


def _load_sticker(index: int, size: int, pool: list[Path] | None = None) -> Image.Image:
    # Stable fallback to built-in sticker_0 when the original index points past
    # the current pool — keeps historical chart cells from re-shuffling when a
    # custom sticker is removed.
    if pool is None:
        pool = _sticker_pool()
    path = pool[index] if 0 <= index < len(pool) else pool[0]
    img = Image.open(path).convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    return img


def _next_custom_path() -> Path:
    existing = {p.name for p in STICKER_DIR.glob(f"{CUSTOM_PREFIX}*.png")}
    n = 1
    while True:
        name = f"{CUSTOM_PREFIX}{n:03d}.png"
        if name not in existing:
            return STICKER_DIR / name
        n += 1


def save_custom_sticker(data: bytes) -> Path:
    """Decode arbitrary image bytes, center-crop to square, resize to STICKER_SIZE,
    and save as a new custom_NNN.png. Persists the bytes to the database as
    well so the file survives Railway redeploys. Returns the saved path."""
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    try:
        src = Image.open(io.BytesIO(data))
        src.load()
    except Exception as e:
        raise ValueError(f"could not read image: {e}") from e
    img = src.convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((STICKER_SIZE, STICKER_SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    blob = buf.getvalue()
    out_path = _next_custom_path()
    out_path.write_bytes(blob)
    storage.save_sticker_blob(out_path.name, blob)
    return out_path


def list_custom_stickers() -> list[Path]:
    return sorted(p for p in STICKER_DIR.glob(f"{CUSTOM_PREFIX}*.png") if p.is_file())


def remove_custom_sticker(name: str) -> bool:
    """Delete a custom sticker by filename. Returns True if deleted.
    Refuses to delete built-in sticker_*.png files."""
    if not name.startswith(CUSTOM_PREFIX) or not name.endswith(".png"):
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    p = STICKER_DIR / name
    file_existed = p.is_file()
    if file_existed:
        p.unlink()
    db_existed = storage.delete_sticker_blob(name)
    return file_existed or db_existed


def hydrate_custom_stickers() -> None:
    """Sync the on-disk sticker cache with the database.

    Restores any DB-stored stickers missing from disk (the case after a
    Railway redeploy wipes the container filesystem), and backfills any
    orphan disk files into the DB (covers SQLite users upgrading, or files
    written before the DB write succeeded). Safe to call once at boot."""
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    blobs = storage.list_sticker_blobs()
    db_names = {name for name, _ in blobs}

    restored = 0
    for name, data in blobs:
        path = STICKER_DIR / name
        if not path.exists() or path.stat().st_size != len(data):
            path.write_bytes(data)
            restored += 1

    backfilled = 0
    for path in STICKER_DIR.glob(f"{CUSTOM_PREFIX}*.png"):
        if path.is_file() and path.name not in db_names:
            storage.save_sticker_blob(path.name, path.read_bytes())
            backfilled += 1

    if restored or backfilled:
        log.info(
            "custom stickers: restored %d from db, backfilled %d to db",
            restored, backfilled,
        )


# ---------- chart rendering ----------

BG = (250, 247, 240)
GRID = (210, 200, 180)
TITLE_FG = (60, 50, 40)
TITLE_INK = (45, 75, 75)
TODAY_INK = (90, 130, 130)
DAY_FG = (90, 80, 70)
MISSED_FG = (180, 60, 60)
PENDING_FG = (140, 130, 120)
LEGEND_FILL = (255, 255, 255, 50)
LEGEND_BORDER = (160, 205, 110, 230)
LEGEND_FG = (55, 70, 70)
CELL_RADIUS = 6


def _draw_legend(draw: ImageDraw.ImageDraw, img: Image.Image,
                 image_w: int, footer_top: int, footer_h: int,
                 taken: int, missed: int, streak: int,
                 font: ImageFont.ImageFont, pool: list) -> None:
    cy = footer_top + footer_h // 2
    icon_size = 18
    gap = 8
    seg_gap = 22
    pill_pad_x = 20

    streak_text = f"Streak {streak} day{'s' if streak != 1 else ''}"
    labels = [f"Taken {taken}", f"Missed {missed}", streak_text]
    seg_widths = [icon_size + gap + int(draw.textlength(lbl, font=font)) for lbl in labels]
    content_w = sum(seg_widths) + seg_gap * 2
    pill_w = content_w + pill_pad_x * 2
    pill_h = 40
    pill_x = (image_w - pill_w) // 2
    pill_y = footer_top + (footer_h - pill_h) // 2

    # Draw onto a separate transparent layer so the fill actually blends
    # with what's underneath — ImageDraw writes literal pixel values, so
    # a translucent fill directly on `img` would just be replaced and then
    # lose its alpha during the final convert("RGB").
    pill_layer = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    ImageDraw.Draw(pill_layer).rounded_rectangle(
        [0, 0, pill_w - 1, pill_h - 1],
        radius=10, fill=LEGEND_FILL, outline=LEGEND_BORDER, width=2,
    )
    img.alpha_composite(pill_layer, (pill_x, pill_y))

    ascent, _ = font.getmetrics()
    text_y = cy - ascent // 2 - 1
    start_x = pill_x + pill_pad_x

    sx = start_x
    sticker = _load_sticker(_legend_taken_sticker_index(pool), icon_size, pool)
    img.paste(sticker, (sx, cy - icon_size // 2), sticker)
    draw.text((sx + icon_size + gap, text_y), labels[0], fill=LEGEND_FG, font=font)

    sx = start_x + seg_widths[0] + seg_gap
    xr = icon_size // 2 - 3
    xcx = sx + icon_size // 2
    draw.line([(xcx - xr, cy - xr), (xcx + xr, cy + xr)], fill=MISSED_FG, width=4)
    draw.line([(xcx - xr, cy + xr), (xcx + xr, cy - xr)], fill=MISSED_FG, width=4)
    draw.text((sx + icon_size + gap, text_y), labels[1], fill=LEGEND_FG, font=font)

    sx = start_x + seg_widths[0] + seg_widths[1] + seg_gap * 2
    _draw_streak_icon(draw, img, sx, cy, icon_size, pool)
    draw.text((sx + icon_size + gap, text_y), labels[2], fill=LEGEND_FG, font=font)


def _draw_streak_icon(draw: ImageDraw.ImageDraw, img: Image.Image,
                      sx: int, cy: int, icon_size: int, pool: list) -> None:
    emoji_font = _load_emoji_font()
    if emoji_font is not None:
        # Noto Color Emoji only renders at its native CBDT size, so draw the
        # glyph onto an oversized scratch canvas (with padding so the sun's
        # outer rays don't get clipped) and resize down to icon_size.
        scratch_pad = EMOJI_FONT_NATIVE_SIZE // 2
        scratch_size = EMOJI_FONT_NATIVE_SIZE + scratch_pad * 2
        scratch = Image.new("RGBA", (scratch_size, scratch_size), (0, 0, 0, 0))
        s_draw = ImageDraw.Draw(scratch)
        try:
            s_draw.text((scratch_pad, scratch_pad), "☀",
                        font=emoji_font, embedded_color=True)
        except (TypeError, ValueError):
            scratch = None
        if scratch is not None:
            bbox = scratch.getbbox()
            if bbox is not None:
                cropped = scratch.crop(bbox)
                cropped = cropped.resize((icon_size, icon_size), Image.LANCZOS)
                img.paste(cropped, (sx, cy - icon_size // 2), cropped)
                return
    sun = _load_sticker(3, icon_size, pool)
    img.paste(sun, (sx, cy - icon_size // 2), sun)


def render_month(year: int, month: int) -> bytes:
    cell = 95
    pad = 44
    cols = 7
    cal = calendar.Calendar(firstweekday=0)  # Monday
    weeks = cal.monthdayscalendar(year, month)
    rows = len(weeks)

    title_h = 76
    weekday_h = 34
    footer_h = 76
    width = pad * 2 + cols * cell
    height = pad * 2 + title_h + weekday_h + rows * cell + footer_h

    img = Image.new("RGBA", (width, height), BG + (255,))
    if BG_PATH.exists():
        try:
            bg = Image.open(BG_PATH).convert("RGBA")
            scale = max(width / bg.width, height / bg.height)
            sw, sh = int(bg.width * scale), int(bg.height * scale)
            bg = bg.resize((sw, sh), Image.LANCZOS)
            ox = (sw - width) // 2
            oy = (sh - height) // 2
            bg = bg.crop((ox, oy, ox + width, oy + height))
            img.paste(bg, (0, 0), bg)
        except Exception as e:
            log.warning("could not load decor background %s: %s", BG_PATH, e)
    draw = ImageDraw.Draw(img, "RGBA")

    title_font = _load_font(36, prefer_serif=True)
    day_label_font = _load_font(17)
    day_num_font = _load_font(15)
    footer_font = _load_font(16)

    # title
    title = f"{calendar.month_name[month]} {year}"
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) // 2, pad + 6), title, fill=TITLE_INK, font=title_font)

    # weekday labels
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    label_y = pad + title_h
    for i, lbl in enumerate(labels):
        x = pad + i * cell
        lw = draw.textlength(lbl, font=day_label_font)
        draw.text((x + (cell - lw) // 2, label_y + 6), lbl, fill=DAY_FG, font=day_label_font)

    # grid + content
    grid_y0 = pad + title_h + weekday_h
    today = datetime.now(TZ).date()
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    logs = storage.status_map(start, end)

    sticker_size = cell - 22
    pool = _sticker_pool()

    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            x0 = pad + c * cell
            y0 = grid_y0 + r * cell
            if day == 0:
                continue
            draw.rounded_rectangle([x0, y0, x0 + cell, y0 + cell],
                                   radius=CELL_RADIUS, outline=GRID, width=1)
            d = date(year, month, day)
            draw.text((x0 + 8, y0 + 5), str(day), fill=DAY_FG, font=day_num_font)
            row = logs.get(d.isoformat())
            cx = x0 + cell // 2
            cy = y0 + cell // 2 + 6
            if row and row["status"] == "taken":
                idx = row["sticker_index"] if row["sticker_index"] is not None else 0
                sticker = _load_sticker(int(idx), sticker_size, pool)
                img.paste(sticker, (cx - sticker_size // 2, cy - sticker_size // 2), sticker)
            elif row and row["status"] == "missed":
                rr = sticker_size // 3
                draw.line([(cx - rr, cy - rr), (cx + rr, cy + rr)], fill=MISSED_FG, width=5)
                draw.line([(cx - rr, cy + rr), (cx + rr, cy - rr)], fill=MISSED_FG, width=5)
            elif row and row["status"] == "pending":
                rr = sticker_size // 3
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                             outline=PENDING_FG, width=4)
            elif d == today:
                draw.rounded_rectangle([x0 + 2, y0 + 2, x0 + cell - 2, y0 + cell - 2],
                                       radius=max(CELL_RADIUS - 2, 2),
                                       outline=TODAY_INK, width=3)

    # footer
    cnt = storage.counts(start, end)
    streak = storage.current_streak()
    _draw_legend(draw, img, width, height - footer_h, footer_h,
                 cnt["taken"], cnt["missed"], streak, footer_font, pool)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def render_week_strip(end_day: Optional[date] = None) -> bytes:
    if end_day is None:
        end_day = datetime.now(TZ).date()
    start_day = end_day - timedelta(days=6)

    cell = 120
    pad = 44
    cols = 7
    title_h = 76
    weekday_h = 34
    legend_gap = 14
    footer_h = 76
    bottom_pad = 255
    width = pad * 2 + cols * cell
    height = pad + title_h + weekday_h + cell + legend_gap + footer_h + bottom_pad

    img = Image.new("RGBA", (width, height), BG + (255,))
    if BG_PATH_WEEK.exists():
        try:
            bg = Image.open(BG_PATH_WEEK).convert("RGBA")
            scale = max(width / bg.width, height / bg.height)
            sw, sh = int(bg.width * scale), int(bg.height * scale)
            bg = bg.resize((sw, sh), Image.LANCZOS)
            ox = (sw - width) // 2
            oy = (sh - height) // 2
            bg = bg.crop((ox, oy, ox + width, oy + height))
            img.paste(bg, (0, 0), bg)
        except Exception as e:
            log.warning("could not load decor background %s: %s", BG_PATH_WEEK, e)
    draw = ImageDraw.Draw(img, "RGBA")

    title_font = _load_font(36, prefer_serif=True)
    day_label_font = _load_font(17)
    day_num_font = _load_font(15)
    footer_font = _load_font(16)

    title = "Last 7 days"
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) // 2, pad + 6), title, fill=TITLE_INK, font=title_font)

    label_y = pad + title_h
    labels = [(start_day + timedelta(days=i)).strftime("%a") for i in range(cols)]
    for i, lbl in enumerate(labels):
        x = pad + i * cell
        lw = draw.textlength(lbl, font=day_label_font)
        draw.text((x + (cell - lw) // 2, label_y + 6), lbl, fill=DAY_FG, font=day_label_font)

    grid_y0 = pad + title_h + weekday_h
    today = datetime.now(TZ).date()
    logs = storage.status_map(start_day, end_day)

    sticker_size = cell - 22
    pool = _sticker_pool()

    for i in range(cols):
        d = start_day + timedelta(days=i)
        x0 = pad + i * cell
        y0 = grid_y0
        draw.rounded_rectangle([x0, y0, x0 + cell, y0 + cell],
                               radius=CELL_RADIUS, outline=GRID, width=1)
        draw.text((x0 + 8, y0 + 5), str(d.day), fill=DAY_FG, font=day_num_font)
        row = logs.get(d.isoformat())
        cx = x0 + cell // 2
        cy = y0 + cell // 2 + 6
        if row and row["status"] == "taken":
            idx = row["sticker_index"] if row["sticker_index"] is not None else 0
            sticker = _load_sticker(int(idx), sticker_size, pool)
            img.paste(sticker, (cx - sticker_size // 2, cy - sticker_size // 2), sticker)
        elif row and row["status"] == "missed":
            rr = sticker_size // 3
            draw.line([(cx - rr, cy - rr), (cx + rr, cy + rr)], fill=MISSED_FG, width=5)
            draw.line([(cx - rr, cy + rr), (cx + rr, cy - rr)], fill=MISSED_FG, width=5)
        elif row and row["status"] == "pending":
            rr = sticker_size // 3
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                         outline=PENDING_FG, width=4)
        elif d == today:
            draw.rounded_rectangle([x0 + 2, y0 + 2, x0 + cell - 2, y0 + cell - 2],
                                   radius=max(CELL_RADIUS - 2, 2),
                                   outline=TODAY_INK, width=3)

    cnt = storage.counts(start_day, end_day)
    streak = storage.current_streak()
    legend_top = pad + title_h + weekday_h + cell + legend_gap
    _draw_legend(draw, img, width, legend_top, footer_h,
                 cnt["taken"], cnt["missed"], streak, footer_font, pool)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
