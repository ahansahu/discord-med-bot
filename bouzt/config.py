"""Module-internal config for the Bouzt Gold feature.

Reads its own env vars rather than importing from the host bot's config so the
package stays self-contained and can later be lifted into a new bot without
edits.
"""
import os
from pathlib import Path


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
DB_PATH = Path(os.getenv("DB_PATH", "med_bot.db"))

_guild = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(_guild) if _guild else None

_admin = os.getenv("TARGET_USER_ID", "").strip()
TARGET_USER_ID = int(_admin) if _admin else None

_chan = os.getenv("BOUZT_CHANNEL_ID", "").strip()
BOUZT_CHANNEL_ID = int(_chan) if _chan else None

STARTING_BALANCE = 1000
CURRENCY_NAME = "Bouzt Gold"
CURRENCY_SHORT = "BG"
CURRENCY_EMOJI = "🪙"

AUTO_LOCK_POLL_SECONDS = 20
MAX_LOCK_DURATION_MINUTES = 60 * 24 * 14  # 14 days
