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

## Deploy to HeavenCloud (free, 24/7)

HeavenCloud is a free Pterodactyl-panel host designed for Discord bots. ~10-minute setup.

### 1. Sign up & create a server

1. Go to https://heavencloud.in and create an account.
2. From the dashboard, click **Create Server** (or similar) and pick the **Python** egg / template.
3. Set:
   - **Memory:** 512 MB is plenty (this bot uses ~80 MB)
   - **Disk:** 1 GB is fine
   - **Region:** EU (closest to your Europe/London users)

### 2. Pull your code from GitHub

In the server's panel:
1. Open the **File Manager** (or use SFTP — credentials are in the panel).
2. Either:
   - **Git method:** open the **Console** tab and run `git clone https://github.com/<you>/discord-med-bot.git .` (the trailing dot puts it in the current dir).
   - **Upload method:** download the repo as a ZIP from GitHub and upload it via the file manager.

### 3. Set environment variables

In the panel, find **Startup** or **Variables** (the location varies — sometimes under **Settings**):
- `DISCORD_TOKEN` — your bot token
- `GUILD_ID` — your server ID
- `CHANNEL_ID` — your reminder channel ID
- `TARGET_USER_ID` — your Discord user ID
- `TIMEZONE` — `Europe/London`
- `UPTIMEROBOT_HEARTBEAT_URL` — leave blank for now (set up later in [Mitigations](#mitigations))

### 4. Start command & dependencies

- **Startup command:** `python bot.py`
- **Dependencies install:** most Pterodactyl Python eggs auto-run `pip install -r requirements.txt` on (re)start. If yours doesn't, open the **Console** and run it manually once.

### 5. Start the bot

Hit **Start** in the panel. Watch the console for:

```
logged in as <your-bot-name> (id=...)
synced 5 guild commands
scheduler started; jobs=['reminder_11', 'reminder_15', ...]
```

Open Discord and run `/status` — you should get a "no entry for today yet" reply.

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
bot.py          # discord client, events, slash commands
scheduler.py    # APScheduler cron jobs + heartbeat / backup / weekly summary
storage.py      # SQLite log + queries
chart.py        # Pillow sticker chart + procedural sticker drawing
config.py       # env loader
assets/stickers # generated on first run
Procfile        # for Heroku-style hosts
runtime.txt     # Python version hint
```
