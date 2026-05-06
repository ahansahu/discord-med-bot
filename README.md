# discord-med-bot

A small Discord bot that asks once a day whether you've taken your medication, nudges you until you confirm, and keeps a sticker-chart record of your hits and misses.

## Features

- Daily reminder at **11:00 Europe/London**, re-pings at **15:00, 19:00, 23:00**, marked missed at **02:00** if still pending.
- Confirm by reacting ✅ on the reminder, replying `yes` / `y` / `taken` / `done` in the channel, or running `/taken`.
- SQLite log of every day's status (`pending` / `taken` / `missed`).
- Slash commands: `/taken`, `/status`, `/week`, `/month` (alias `/chart`).
- Procedurally-drawn sticker chart (PNG) — random fun sticker (star, heart, smiley, sun, sparkle, flower) on every taken day.
- Single-target tracking: only one configured user can confirm; everyone else is ignored politely.

## Quick start (local)

1. Install Python 3.11+.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` — bot token from the [Discord Developer Portal](https://discord.com/developers/applications).
   - `GUILD_ID` — your server's ID (right-click server → Copy Server ID; needs Developer Mode on).
   - `CHANNEL_ID` — the channel the bot will post reminders in.
   - `TARGET_USER_ID` — your Discord user ID (right-click yourself → Copy User ID).
4. Make sure the bot has these intents enabled in the developer portal: **Message Content** + **Server Members** (presence not needed).
5. Invite the bot to your server with the `bot` and `applications.commands` scopes and the `Send Messages`, `Read Message History`, `Add Reactions`, `Use Slash Commands` permissions.
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

## Hosting on a free Discord-bot host (24/7 always-on)

Hosts that work in 2026 (free, no inactivity sleep):
- HeavenCloud — `https://heavencloud.in/service/free-discord-bot-hosting`
- JustRunMy.App — `https://justrunmy.app/discord-bots`
- FreeVPS.edu.pl — `https://freevps.edu.pl/free-vps-discord-bot-hosting.php`

Generic steps (the dashboards differ slightly):

1. Push this repo to GitHub (private is fine).
2. On the host, create a new **Python** bot project and link the repo.
3. Set the start command to `python bot.py`.
4. Set the env vars from your `.env` in the host's dashboard.
5. Make sure the host gives you a **persistent disk** for `med_bot.db` and `assets/stickers/`. If not, point `DB_PATH` at a mounted volume, or fall back to a hosted SQLite (e.g. Turso free tier) — see the "Persistence fallback" section below.
6. Start the project. Check the logs for `logged in as ...` and `scheduler started`.

> **Note on Render / Railway:** Render's free web services sleep after 15 min of inactivity (which kills the Discord websocket), and Railway no longer has a free tier — only a one-time $5 credit. They're not viable for free 24/7. Use one of the Discord-bot-specialized hosts above, or pay ~$5/mo on Railway for the easiest deploy.

## Persistence fallback

If your host doesn't give a persistent disk:
- Switch to a hosted SQLite — the simplest is [Turso](https://turso.tech) (libSQL, free tier).
- Replace `storage._conn()` with a libSQL connection (`pip install libsql-experimental` or `libsql-client`) and update `DB_PATH` to a `libsql://` URL.
- Everything else (schema, queries) works unchanged.

## Customizing the schedule

The reminder hours and cutoff live in `config.py`:

```python
REMINDER_HOURS = (11, 15, 19, 23)
MISSED_CUTOFF_HOUR = 2
```

Change those constants and restart the bot. The scheduler uses `Europe/London` from `TIMEZONE` in `.env`, so DST is handled for you.

## Project layout

```
bot.py          # discord client, events, slash commands
scheduler.py    # APScheduler cron jobs
storage.py      # SQLite log + queries
chart.py        # Pillow sticker chart + procedural sticker drawing
config.py       # env loader
assets/stickers # generated on first run
```
