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

## Todo channel (optional)

If `TODO_CHANNEL_ID` is set, the bot owns a single pinned message in that
channel that holds your todo list. You can mutate it through either slash
commands or buttons + modals on the pinned message itself — both paths share
the same storage.

**Setup:** set `TODO_CHANNEL_ID` in `.env` (or Railway env vars) to a channel ID
the bot can post in. The bot needs `Manage Messages` (to pin) on top of its
existing permissions. Restart — on first run the bot posts a placeholder
message and pins it.

**Pinned message format:**

```
Buy-

Treats for Mu
Shampoo

Errands-

White pens
```

One row per section header (`name-`), blank line, then items. Empty sections
are hidden until you re-add items.

**Buttons on the pinned message:**

- Click an item → marks done (removes it).
- `➕ Add` → modal: enter item text and an optional section (created if new).
- `🗂 Sections` → ephemeral menu to rename, reorder, delete, or create sections.
- `↕ Edit` → enters reorder mode: click an item to select it, then use
  `⬆ Top / ▲ Up / ▼ Down / ⬇ Bottom` to move it. `✕ Exit` returns to normal.
- `‹ ›` → pagination when the list exceeds 20 items.

**Slash commands** (everything is also reachable through buttons):

| Command | Description |
| ------- | ----------- |
| `/todo add item:<text> section:<name?>` | Add an item. Section is created if it doesn't exist; defaults to the first section. |
| `/todo done item:<text>` | Mark an item done (autocomplete). |
| `/todo move item:<text> direction:<up\|down>` | Move an item within its section. |
| `/todo priority item:<text> level:<high\|low>` | Jump an item to the top / bottom of its section. |
| `/todo section add name:<name>` | Create a section. |
| `/todo section remove name:<name>` | Delete a section and its items. |
| `/todo section rename old:<name> new:<name>` | Rename a section. |
| `/todo section move name:<name> direction:<up\|down>` | Reorder sections. |

## Deploy to Railway (24/7, ~$3–5/month)

[Railway](https://railway.app) is the simplest path to a reliable, always-on Discord bot. Auto-deploys on every git push, real persistent storage, no sleep policies. New accounts get a one-time **$5 trial credit** that covers about a month for this bot, then ~$3–5/month after.

### 1. Sign up

- Go to https://railway.app → **Login with GitHub**
- Authorize Railway to read your GitHub repos (you can scope to just this one)

### 2. Create the project

- Dashboard → **+ New Project** → **Deploy from GitHub repo**
- Pick `ahansahu/discord-med-bot`
- Railway auto-detects Python via `requirements.txt`, runs `pip install`, and reads `Procfile` / `railway.json` (already in this repo) to find the start command (`python bot.py`)

The first build takes ~1–2 min. **Don't worry that it errors on the first deploy** — it'll fail because env vars aren't set yet; that's expected.

### 3. Add a Postgres database (critical — don't skip)

Without persistence, your medication log resets every redeploy. Railway gives you a free managed Postgres database that the bot uses automatically.

1. On your project canvas, click **+ Create**
2. Select **Database** → **PostgreSQL**
3. Wait ~10 seconds for it to provision
4. Railway automatically injects a `DATABASE_URL` env var into your bot service — you don't need to copy it

That's it. The bot detects `DATABASE_URL` at startup and uses Postgres instead of SQLite.

### 4. Set environment variables

In the project → click the bot service → **Variables** tab → add each:

| Variable | Value |
|----------|-------|
| `DISCORD_TOKEN` | your bot token |
| `GUILD_ID` | your server ID |
| `CHANNEL_ID` | your reminder channel ID |
| `TODO_CHANNEL_ID` | (optional — enables the [todo channel](#todo-channel-optional)) |
| `TARGET_USER_ID` | your Discord user ID |
| `TIMEZONE` | `Europe/London` |
| `UPTIMEROBOT_HEARTBEAT_URL` | (optional — see [Mitigations](#mitigations)) |

`DATABASE_URL` is set automatically by Railway when you add Postgres — don't add it manually.

### 5. Redeploy

After setting variables, Railway auto-redeploys. Watch the **Deployments** → **Logs** tab for:

```
logged in as <your-bot-name> (id=...)
synced 5 guild commands
scheduler started; jobs=['reminder_11', ...]
```

### 6. Verify

In your Discord channel, run `/status` — should reply "no entry for today yet — first reminder fires at 11:00."

### Updating the bot later

Just `git push` to GitHub. Railway watches the repo and auto-redeploys (~30s, transparent to you). No SSH, no manual restart.

### Cost monitoring

In your Railway dashboard → **Usage** tab → see live cost trending. This bot runs ~80 MB RAM steady-state, so you'll see ~$0.10/day after the trial credit runs out. Set up a billing alert at $5/month so you're never surprised.

### If you want to drop off Railway later

When the trial credit runs out, your options are:
- Add a card and pay ~$3–5/mo (easiest)
- Switch to Oracle Cloud Always Free (this repo had setup scripts for that — see git history)
- Switch to a free Discord-bot host like JustRunMy.App or Discloud (re-add a `discloud.config` etc.)

Your weekly DB backup DM (see [Mitigations](#mitigations)) makes any of these migrations painless — drop the backup file at `med_bot.db` on the new host and your full history is back.

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

The bot DMs you a weekly backup of your log (see [Mitigations](#mitigations)). Format depends on the backend:

- **Postgres backend (Railway):** weekly DMs are JSON dumps (`med_bot_backup_<date>.json`). Restore by inserting each row back into `daily_log`. Railway also keeps automatic Postgres snapshots, so this is a belt-and-braces copy.
- **SQLite backend (local / file-based hosts):** weekly DMs are the `.db` file directly. Drop it next to `bot.py` (or at `$DB_PATH`) and restart.

### Restoring a JSON dump into Postgres

On a fresh Railway Postgres, connect via the Railway CLI:

```bash
railway run psql $DATABASE_URL
```

Then for each row in the JSON, run:

```sql
INSERT INTO daily_log (date, status, taken_at, sticker_index, reminder_msg_id)
VALUES ('2026-04-01', 'taken', '2026-04-01T11:23:00+01:00', 2, NULL);
```

(Or write a small Python script that reads the JSON and inserts via `psycopg`.)

### Migrating between backends

The schema is identical between SQLite and Postgres, so you can `pg_dump` from one and `pg_restore` to the other (or read SQLite via a script and bulk-insert). Both backends support the same set of statuses (`pending` / `taken` / `missed`) and column types.

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
todo.py           # pinned todo list: rendering, persistent buttons, modals
config.py         # env loader
Procfile          # start command for Railway / nixpacks
railway.json      # Railway build + deploy config
assets/stickers/  # generated on first run
```
