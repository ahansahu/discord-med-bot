import calendar
import io
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from config import STICKER_DIR, TZ
import storage


STICKER_SIZE = 96
STICKER_COUNT = 6


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


def random_sticker_index() -> int:
    _ensure_stickers()
    return random.randrange(STICKER_COUNT)


def _load_sticker(index: int, size: int) -> Image.Image:
    paths = _ensure_stickers()
    img = Image.open(paths[index % len(paths)]).convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    return img


# ---------- chart rendering ----------

BG = (250, 247, 240)
GRID = (210, 200, 180)
TITLE_FG = (60, 50, 40)
DAY_FG = (90, 80, 70)
MISSED_FG = (180, 60, 60)
PENDING_FG = (140, 130, 120)


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

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    title_font = _load_font(38)
    day_label_font = _load_font(20)
    day_num_font = _load_font(18)
    footer_font = _load_font(20)

    # title
    title = f"{calendar.month_name[month]} {year}"
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) // 2, pad + 6), title, fill=TITLE_FG, font=title_font)

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

    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            x0 = pad + c * cell
            y0 = grid_y0 + r * cell
            draw.rectangle([x0, y0, x0 + cell, y0 + cell], outline=GRID, width=1)
            if day == 0:
                continue
            d = date(year, month, day)
            # day number
            draw.text((x0 + 6, y0 + 4), str(day), fill=DAY_FG, font=day_num_font)
            row = logs.get(d.isoformat())
            cx = x0 + cell // 2
            cy = y0 + cell // 2 + 6
            if row and row["status"] == "taken":
                idx = row["sticker_index"] if row["sticker_index"] is not None else 0
                sticker = _load_sticker(int(idx), sticker_size)
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
                draw.rectangle([x0 + 2, y0 + 2, x0 + cell - 2, y0 + cell - 2],
                               outline=(120, 144, 156), width=3)

    # footer
    cnt = storage.counts(start, end)
    streak = storage.current_streak()
    footer = f"Taken {cnt['taken']}  ·  Missed {cnt['missed']}  ·  Streak {streak} day{'s' if streak != 1 else ''}"
    fw = draw.textlength(footer, font=footer_font)
    draw.text(((width - fw) // 2, height - footer_h + 12), footer, fill=TITLE_FG, font=footer_font)

    out = io.BytesIO()
    img.save(out, "PNG")
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

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    title_font = _load_font(28)
    label_font = _load_font(18)
    footer_font = _load_font(18)

    title = "Last 7 days"
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) // 2, pad + 6), title, fill=TITLE_FG, font=title_font)

    logs = storage.status_map(start_day, end_day)
    sticker_size = cell - 24

    for i in range(7):
        d = start_day + timedelta(days=i)
        x0 = pad + i * cell
        y0 = pad + title_h
        # label
        lbl = d.strftime("%a %d")
        lw = draw.textlength(lbl, font=label_font)
        draw.text((x0 + (cell - lw) // 2, y0 + 4), lbl, fill=DAY_FG, font=label_font)
        # cell
        cy0 = y0 + label_h
        draw.rectangle([x0, cy0, x0 + cell, cy0 + cell], outline=GRID, width=1)
        cx = x0 + cell // 2
        cy = cy0 + cell // 2
        row = logs.get(d.isoformat())
        if row and row["status"] == "taken":
            idx = row["sticker_index"] if row["sticker_index"] is not None else 0
            sticker = _load_sticker(int(idx), sticker_size)
            img.paste(sticker, (cx - sticker_size // 2, cy - sticker_size // 2), sticker)
        elif row and row["status"] == "missed":
            rr = sticker_size // 3
            draw.line([(cx - rr, cy - rr), (cx + rr, cy + rr)], fill=MISSED_FG, width=6)
            draw.line([(cx - rr, cy + rr), (cx + rr, cy - rr)], fill=MISSED_FG, width=6)
        elif row and row["status"] == "pending":
            rr = sticker_size // 3
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                         outline=PENDING_FG, width=4)

    cnt = storage.counts(start_day, end_day)
    streak = storage.current_streak()
    footer = f"Taken {cnt['taken']} / 7  ·  Streak {streak}"
    fw = draw.textlength(footer, font=footer_font)
    draw.text(((width - fw) // 2, height - footer_h + 10), footer, fill=TITLE_FG, font=footer_font)

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
