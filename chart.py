import calendar
import io
import logging
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import STICKER_DIR, TZ
import storage


log = logging.getLogger("med_bot.chart")

STICKER_SIZE = 96
STICKER_COUNT = 6
CUSTOM_PREFIX = "custom_"


# ---------- font loading ----------

def _load_font(size: int) -> ImageFont.ImageFont:
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

# Beachy palette: sand + sea + sky + coral.
SAND_TOP = (224, 238, 244)      # pale sky at the top of the canvas
SAND_MID = (252, 244, 224)      # warm sand
SAND_BOTTOM = (240, 220, 188)   # deeper sand at the bottom
SEA = (138, 196, 208)           # sea-foam teal — header band, wave
SEA_DEEP = (62, 120, 138)       # deep teal — title text, footer text
CELL_FILL = (255, 251, 242)     # soft cream day-cards
CELL_SHADOW = (170, 140, 100)   # warm shadow under cards
GRID_SOFT = (220, 198, 170)     # very soft cell outline
CORAL = (240, 140, 110)         # today pill, accent
TITLE_FG = SEA_DEEP
DAY_FG = (96, 82, 66)
MISSED_FG = (200, 90, 80)
PENDING_FG = (150, 140, 125)


def _paint_sand_gradient(img: Image.Image) -> None:
    w, h = img.size
    draw = ImageDraw.Draw(img)
    mid = int(h * 0.32)
    for y in range(h):
        if y <= mid:
            t = y / max(mid, 1)
            r = int(SAND_TOP[0] + (SAND_MID[0] - SAND_TOP[0]) * t)
            g = int(SAND_TOP[1] + (SAND_MID[1] - SAND_TOP[1]) * t)
            b = int(SAND_TOP[2] + (SAND_MID[2] - SAND_TOP[2]) * t)
        else:
            t = (y - mid) / max(h - 1 - mid, 1)
            r = int(SAND_MID[0] + (SAND_BOTTOM[0] - SAND_MID[0]) * t)
            g = int(SAND_MID[1] + (SAND_BOTTOM[1] - SAND_MID[1]) * t)
            b = int(SAND_MID[2] + (SAND_BOTTOM[2] - SAND_MID[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b, 255))


def _draw_card(
    base: Image.Image,
    box: list,
    radius: int = 14,
    fill: tuple = CELL_FILL,
    outline: tuple = GRID_SOFT,
    outline_width: int = 1,
    shadow: bool = True,
) -> None:
    x0, y0, x1, y1 = box
    if shadow:
        pad = 8
        offset = 3
        layer = Image.new("RGBA", (x1 - x0 + pad * 2, y1 - y0 + pad * 2), (0, 0, 0, 0))
        sd = ImageDraw.Draw(layer)
        sd.rounded_rectangle(
            [pad, pad, pad + (x1 - x0), pad + (y1 - y0)],
            radius=radius,
            fill=(*CELL_SHADOW, 110),
        )
        layer = layer.filter(ImageFilter.GaussianBlur(radius=3))
        base.alpha_composite(layer, dest=(x0 - pad + offset, y0 - pad + offset))
    ImageDraw.Draw(base).rounded_rectangle(
        box, radius=radius, fill=fill, outline=outline, width=outline_width
    )


def _draw_wash(
    base: Image.Image,
    box: list,
    radius: int,
    color: tuple,
    alpha: int,
) -> None:
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [0, 0, x1 - x0, y1 - y0], radius=radius, fill=(*color, alpha)
    )
    base.alpha_composite(layer, dest=(x0, y0))


def _draw_wave(
    draw: ImageDraw.ImageDraw,
    x0: int,
    x1: int,
    y: int,
    color: tuple,
    amp: int = 4,
    period: int = 28,
    width: int = 2,
) -> None:
    points = []
    x = x0
    while x <= x1:
        wy = y + amp * math.sin(2 * math.pi * (x - x0) / period)
        points.append((x, wy))
        x += 2
    if len(points) >= 2:
        draw.line(points, fill=color, width=width, joint="curve")


def _draw_today_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    font: ImageFont.ImageFont,
    fill: tuple = CORAL,
    fg: tuple = (255, 255, 255),
) -> None:
    tw = draw.textlength(label, font=font)
    asc, desc = font.getmetrics()
    th = asc + desc
    pad_x = 6
    pad_y = 1
    radius = (th + pad_y * 2) // 2
    draw.rounded_rectangle(
        [x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y],
        radius=radius,
        fill=fill,
    )
    draw.text((x, y), label, fill=fg, font=font)


def render_month(year: int, month: int) -> bytes:
    cell = 110
    pad = 16
    cols = 7
    cal = calendar.Calendar(firstweekday=0)  # Monday
    weeks = cal.monthdayscalendar(year, month)
    rows = len(weeks)

    title_h = 70
    weekday_h = 36
    footer_h = 50
    width = pad * 2 + cols * cell
    height = pad * 2 + title_h + weekday_h + rows * cell + footer_h

    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    _paint_sand_gradient(img)
    draw = ImageDraw.Draw(img)

    title_font = _load_font(38)
    day_label_font = _load_font(20)
    day_num_font = _load_font(18)
    footer_font = _load_font(20)

    # header wash
    _draw_wash(img, [pad, pad, width - pad, pad + title_h - 6], radius=20, color=SEA, alpha=95)

    # title
    title = f"{calendar.month_name[month]} {year}"
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) // 2, pad + 8), title, fill=TITLE_FG, font=title_font)

    # wave divider between header and weekday row
    _draw_wave(draw, pad + 8, width - pad - 8, pad + title_h - 2, SEA_DEEP, amp=3, period=26, width=2)

    # weekday labels with a soft cream strip behind
    label_y = pad + title_h
    _draw_card(
        img,
        [pad, label_y + 2, width - pad, label_y + weekday_h - 2],
        radius=14,
        fill=CELL_FILL,
        outline=GRID_SOFT,
        shadow=False,
    )
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, lbl in enumerate(labels):
        x = pad + i * cell
        lw = draw.textlength(lbl, font=day_label_font)
        draw.text((x + (cell - lw) // 2, label_y + 6), lbl, fill=SEA_DEEP, font=day_label_font)

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
            # rounded cream card with soft shadow; 3px inset lets the gradient peek through
            _draw_card(
                img,
                [x0 + 3, y0 + 3, x0 + cell - 3, y0 + cell - 3],
                radius=14,
                fill=CELL_FILL,
                outline=GRID_SOFT,
                shadow=True,
            )
            d = date(year, month, day)
            if d == today:
                _draw_today_pill(draw, x0 + 8, y0 + 6, str(day), day_num_font)
            else:
                draw.text((x0 + 8, y0 + 6), str(day), fill=DAY_FG, font=day_num_font)
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

    # wave above footer
    _draw_wave(draw, pad + 8, width - pad - 8, height - footer_h + 4, SEA_DEEP, amp=3, period=26, width=2)

    # footer
    cnt = storage.counts(start, end)
    streak = storage.current_streak()
    footer = f"Taken {cnt['taken']}  ·  Missed {cnt['missed']}  ·  Streak {streak} day{'s' if streak != 1 else ''}"
    fw = draw.textlength(footer, font=footer_font)
    draw.text(((width - fw) // 2, height - footer_h + 14), footer, fill=SEA_DEEP, font=footer_font)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def render_week_strip(end_day: Optional[date] = None) -> bytes:
    if end_day is None:
        end_day = datetime.now(TZ).date()
    start_day = end_day - timedelta(days=6)

    cell = 120
    pad = 16
    label_h = 32
    title_h = 56
    footer_h = 44
    width = pad * 2 + 7 * cell
    height = pad * 2 + title_h + label_h + cell + footer_h

    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    _paint_sand_gradient(img)
    draw = ImageDraw.Draw(img)

    title_font = _load_font(28)
    label_font = _load_font(18)
    footer_font = _load_font(18)

    # header wash + title
    _draw_wash(img, [pad, pad, width - pad, pad + title_h - 6], radius=18, color=SEA, alpha=95)
    title = "Last 7 days"
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) // 2, pad + 8), title, fill=TITLE_FG, font=title_font)

    # wave divider between header and labels
    _draw_wave(draw, pad + 8, width - pad - 8, pad + title_h - 2, SEA_DEEP, amp=3, period=26, width=2)

    # labels strip (one cream card spanning the labels row)
    label_y = pad + title_h
    _draw_card(
        img,
        [pad, label_y + 2, width - pad, label_y + label_h - 2],
        radius=12,
        fill=CELL_FILL,
        outline=GRID_SOFT,
        shadow=False,
    )

    logs = storage.status_map(start_day, end_day)
    sticker_size = cell - 24
    pool = _sticker_pool()
    today = datetime.now(TZ).date()

    for i in range(7):
        d = start_day + timedelta(days=i)
        x0 = pad + i * cell
        y0 = pad + title_h
        lbl = d.strftime("%a %d")
        lw = draw.textlength(lbl, font=label_font)
        label_x = int(x0 + (cell - lw) // 2)
        if d == today:
            _draw_today_pill(draw, label_x, y0 + 6, lbl, label_font)
        else:
            draw.text((label_x, y0 + 6), lbl, fill=SEA_DEEP, font=label_font)
        cy0 = y0 + label_h
        _draw_card(
            img,
            [x0 + 4, cy0 + 4, x0 + cell - 4, cy0 + cell - 4],
            radius=16,
            fill=CELL_FILL,
            outline=GRID_SOFT,
            shadow=True,
        )
        cx = x0 + cell // 2
        cy = cy0 + cell // 2
        row = logs.get(d.isoformat())
        if row and row["status"] == "taken":
            idx = row["sticker_index"] if row["sticker_index"] is not None else 0
            sticker = _load_sticker(int(idx), sticker_size, pool)
            img.paste(sticker, (cx - sticker_size // 2, cy - sticker_size // 2), sticker)
        elif row and row["status"] == "missed":
            rr = sticker_size // 3
            draw.line([(cx - rr, cy - rr), (cx + rr, cy + rr)], fill=MISSED_FG, width=6)
            draw.line([(cx - rr, cy + rr), (cx + rr, cy - rr)], fill=MISSED_FG, width=6)
        elif row and row["status"] == "pending":
            rr = sticker_size // 3
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                         outline=PENDING_FG, width=4)

    # wave above footer
    _draw_wave(draw, pad + 8, width - pad - 8, height - footer_h + 4, SEA_DEEP, amp=3, period=26, width=2)

    cnt = storage.counts(start_day, end_day)
    streak = storage.current_streak()
    footer = f"Taken {cnt['taken']} / 7  ·  Streak {streak}"
    fw = draw.textlength(footer, font=footer_font)
    draw.text(((width - fw) // 2, height - footer_h + 12), footer, fill=SEA_DEEP, font=footer_font)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
