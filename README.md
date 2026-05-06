# discord-med-bot

A small Discord bot that asks once a day whether you've taken your medication, nudges you until you confirm, and keeps a sticker-chart record of your hits and misses.

## Features

- Daily reminder at **11:00 Europe/London**, re-pings at **15:00, 19:00, 23:00**, marked missed at **02:00** if still pending.
- Confirm by reacting ✅ on the reminder, replying `yes` / `y` / `taken` / `done` in the channel, or running `/taken`.
- SQLite log of every day's status (`pending` / `taken` / `missed`).
- Slash commands: `/taken`, `/status`, `/week`, `/month` (alias `/chart`).
- Procedurally-drawn sticker chart (PNG) — random fun sticker (star, heart, smiley, sun, sparkle, flower) on every taken day.
- Single-target tracking: only one configured user can confirm; everyone else is ignored politely.
- **Built-in mitigations** for free-host flakiness — see [Mitigations](#mitigations).

## Quick start (local)

1. Install Python 3.11+.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` — bot token from the [Discord Developer Portal](https://discord.com/developers/applications).
   - `GUILD_ID` — your server's ID (right-click server → Copy Server ID; needs Developer Mode on).
   - `CHANNEL_ID` — the channel the bot will post reminders in.
   - `TARGET_USER_ID` — your Discord user ID (right-click yourself → Copy User ID).
4. Make sure the bot has the **Message Content Intent** enabled in the developer portal.
5. Invite the bot with the `bot` and `applications.commands` scopes and the `Send Messages`, `Read Message History`, `Add Reactions`, `Use Slash Commands`, `Attach Files` permissions.
6. `python bot.py`

The first run creates `med_bot.db` and procedurally generates the 6 sticker PNGs in `assets/stickers/`.

## Slash commands

| Command   | Description                                                  |
| --------- | ------------------------------------------------------------ |
| `/taken`  | Mark today as taken                                          |
| `/status` | Today's status + current streak                              |
| `/week`   | Last-7-day strip image with stats                            |
| `/month`  | Current month's full sticker chart with stats                |
| `/chart`  | Alias for `/month`                                           |

## Deploy to Discloud (free, 24/7)

[Discloud](https://discloud.com) is a Discord-bot-specialized free host that's been running since ~2018. Free tier: **100 MB RAM**, 24/7 uptime, persistent storage. ~5-minute setup.

> ⚠️ **RAM note:** 100 MB is tight for a discord.py + Pillow bot. Idle usage is ~70–90 MB, with chart rendering pushing toward the limit. If the bot ever OOMs during `/month`, see [RAM tuning](#ram-tuning-if-discloud-100-mb-is-too-tight) below for fixes.

### 1. Sign up

- Go to https://discloud.com → **Login with Discord** (uses your existing Discord account)
- Authorize the OAuth scopes
- You're dropped into your dashboard

### 2. The config file is already in this repo

`discloud.config` at the project root tells Discloud how to run the bot:

```
ID=med-bot
TYPE=bot
MAIN=bot.py
RAM=100
AUTORESTART=true
VERSION=latest
APT=tools
```

Discloud auto-detects `requirements.txt` and runs `pip install` on first boot.

### 3. Deploy — pick one method

**Option A — Web upload (no CLI):**

1. Zip the project folder (Windows: right-click → Send to → Compressed folder).
2. **Important:** delete `.env` from the zip first — env vars are set in the dashboard.
3. In the Discloud dashboard, click **+ Upload App** and drag the zip in.

**Option B — CLI (faster for future updates):**

```bash
npm install -g discloud
discloud login
discloud commit
```

For future updates, just `git pull` then `discloud commit` again from the project directory.

### 4. Set environment variables

In your Discloud dashboard → click the app → **Variables** tab → add:

- `DISCORD_TOKEN`
- `GUILD_ID`
- `CHANNEL_ID`
- `TARGET_USER_ID`
- `TIMEZONE` = `Europe/London`
- `UPTIMEROBOT_HEARTBEAT_URL` (optional — see [Mitigations](#mitigations))

### 5. Start

Click **Start** in the dashboard. Watch the **Logs** tab for:

```
logged in as <your-bot-name> (id=...)
synced 5 guild commands
scheduler started; jobs=['reminder_11', 'reminder_15', ...]
```

Run `/status` in your Discord channel to confirm.

---

## Mitigations

Free hosts can disappear, restart, or silently die. These three layers make sure you'll know if that happens and won't lose your data.

### A. UptimeRobot heartbeat (recommended)

Get an email alert if the bot goes more than ~1 hour without checking in.

1. Sign up at https://uptimerobot.com (free for 50 monitors).
2. Click **+ New monitor**.
3. Type: **Heartbeat** (some plans call this "Cron Job Monitoring" or similar).
4. Friendly name: `discord-med-bot`
5. Heartbeat interval: **60 minutes** (gives a buffer over our 30-min ping)
6. Save — UptimeRobot gives you a unique URL like `https://heartbeat.uptimerobot.com/abc123`.
7. Add that URL to your bot's env vars as `UPTIMEROBOT_HEARTBEAT_URL`.
8. Restart the bot.

The bot now `GET`s that URL every 30 minutes. If UptimeRobot doesn't see a ping for 60 minutes, it emails you.

### B. Weekly DB backups (automatic)

Every **Sunday at 23:00 London time**, the bot DMs you `med_bot_backup_<date>.db`.

Save these — if your host ever dies, drop the most recent backup next to `bot.py` as `med_bot.db` on a new host and your full history is back.

> **Important:** for the DM to work, you must share a server with the bot (you do) and have **"Allow direct messages from server members"** enabled in your Discord privacy settings (User Settings → Privacy & Safety).

### C. Weekly "I'm alive" post (passive)

Every **Monday at 10:00 London time**, the bot posts a weekly check-in summary in your reminder channel. If you ever notice you didn't get one on a Monday, that's a strong signal the bot is down — go check.

---

## RAM tuning (if Discloud 100 MB is too tight)

If the bot crashes with out-of-memory errors (typically during `/month` chart rendering), try these in order:

### 1. Lazy-load Pillow

Pillow is currently imported at the top of `chart.py` and loads at startup. Move the imports inside the render functions so they only load when a chart is actually requested:

```python
# in chart.py — replace top-level Pillow imports with lazy ones
def render_month(year, month):
    from PIL import Image, ImageDraw, ImageFont
    # ... rest of function
```

Saves ~10–20 MB of steady-state memory.

### 2. Shrink the chart resolution

Edit `chart.py`:

```python
# was: cell = 110
cell = 80   # smaller cells = smaller image = less Pillow working memory
```

The image will be smaller in Discord but still readable.

### 3. Free Pillow buffers explicitly

After rendering, add `gc.collect()` and `del img, draw` to release memory back to the OS faster:

```python
out = io.BytesIO()
img.save(out, "PNG")
data = out.getvalue()
del img, draw, out
import gc; gc.collect()
return data
```

### 4. Last resort — switch hosts

If 100 MB really won't fit (it should), you've outgrown Discloud. Move to:
- **Oracle Cloud Always Free** (24 GB RAM, permanent free)
- **Sparked Host** free tier (1 GB RAM)
- **Railway** ($3–5/mo)

Your weekly DB backup makes the migration painless.

---

## Restoring from a backup

If you have to redeploy (host died, switching providers, etc.):

1. Set up the bot on the new host using the steps above, but **don't start it yet**.
2. Drop your most recent `med_bot_backup_<date>.db` into the bot's working directory and rename it to `med_bot.db`.
3. Start the bot. It'll pick up the existing data — `/month` should show all your old stickers.

## Customizing the schedule

The reminder hours and cutoff live in `config.py`:

```python
REMINDER_HOURS = (11, 15, 19, 23)
MISSED_CUTOFF_HOUR = 2
```

Change those constants and restart the bot. The scheduler uses `Europe/London` so DST is handled for you.

## Project layout

```
bot.py            # discord client, events, slash commands
scheduler.py      # APScheduler cron jobs + heartbeat / backup / weekly summary
storage.py        # SQLite log + queries
chart.py          # Pillow sticker chart + procedural sticker drawing
config.py         # env loader
discloud.config   # Discloud host config
assets/stickers/  # generated on first run
```
