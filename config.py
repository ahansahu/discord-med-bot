import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _int(name: str) -> int:
    return int(_required(name))


DISCORD_TOKEN = _required("DISCORD_TOKEN")
GUILD_ID = _int("GUILD_ID")
CHANNEL_ID = _int("CHANNEL_ID")
TARGET_USER_ID = _int("TARGET_USER_ID")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/London")
TZ = ZoneInfo(TIMEZONE_NAME)

DB_PATH = Path(os.getenv("DB_PATH", "med_bot.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
ASSETS_DIR = Path(__file__).parent / "assets"
STICKER_DIR = ASSETS_DIR / "stickers"

REMINDER_HOURS = (11, 15, 19, 23)
MISSED_CUTOFF_HOUR = 2

CONFIRM_REACTIONS = {"✅", "☑️", "\U0001f44d"}

# Optional: external uptime monitoring. Leave blank to disable.
UPTIMEROBOT_HEARTBEAT_URL = os.getenv("UPTIMEROBOT_HEARTBEAT_URL", "").strip()
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "30"))

# Mitigation schedule (Europe/London)
WEEKLY_SUMMARY_DOW = 0   # Monday=0
WEEKLY_SUMMARY_HOUR = 10
WEEKLY_BACKUP_DOW = 6    # Sunday=6
WEEKLY_BACKUP_HOUR = 23
