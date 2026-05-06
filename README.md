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

## Deploy to Oracle Cloud Always Free (free forever, 24/7)

[Oracle Cloud Always Free](https://www.oracle.com/cloud/free/) gives you a permanent ARM VM (up to 4 cores, 24 GB RAM, 200 GB block storage). Real cloud infrastructure, no inactivity sleep, won't run out of free slots like the small Discord-bot hosts. The trade-off is ~5–10 minutes of one-time Linux setup, which the bundled `oracle-setup.sh` script automates.

### 1. Sign up

- Go to https://www.oracle.com/cloud/free/ → **Start for free**
- You'll need an email, phone number (SMS verification), and a credit card **for identity verification only** — Always Free resources never get charged. You can also enable a hard "no upgrade" cap so it can't bill you even by accident.
- **Pick your Home Region carefully — it's permanent.** For Europe/London, choose **UK South (London)** (`uk-london-1`).
- Verification typically takes 5–60 minutes; sometimes longer.

### 2. Create an Always Free ARM VM

In the Oracle Cloud Console:

1. **Menu** → **Compute** → **Instances** → **Create Instance**
2. **Name:** `med-bot`
3. **Image:** Canonical **Ubuntu 22.04** (or 24.04)
4. **Shape:** click **Change shape** → **Ampere** → `VM.Standard.A1.Flex` → set to **1 OCPU + 6 GB memory**. The "Always Free Eligible" badge must be visible.
5. **Networking:** accept defaults (creates a public-IP VCN).
6. **SSH keys:** either paste your public key or download Oracle's generated keypair — keep the private key file safe.
7. Hit **Create**. Provisioning takes ~1–2 minutes.

Note the public IP from the instance page.

### 3. SSH in

From your local terminal (PowerShell on Windows works fine):

```powershell
ssh -i C:\path\to\ssh-key.key ubuntu@<your-public-ip>
```

### 4. Run the setup script

Once you're SSH'd in:

```bash
curl -O https://raw.githubusercontent.com/ahansahu/discord-med-bot/master/oracle-setup.sh
chmod +x oracle-setup.sh
./oracle-setup.sh
```

The script:
- Installs Python, git, build tools
- Clones this repo to `~/discord-med-bot`
- Creates a Python venv and installs requirements
- Generates a `.env` from the template
- Writes a `systemd` service unit and enables it on boot

It takes ~3 minutes and is safe to re-run if anything goes wrong.

### 5. Fill in your env vars

```bash
nano ~/discord-med-bot/.env
```

Set `DISCORD_TOKEN`, `GUILD_ID`, `CHANNEL_ID`, `TARGET_USER_ID`. `TIMEZONE` is already pinned to `Europe/London`. Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

### 6. Start the bot

```bash
sudo systemctl start med-bot
journalctl -u med-bot -f
```

You should see:

```
logged in as <your-bot-name> (id=...)
synced 5 guild commands
scheduler started; jobs=['reminder_11', ...]
```

Press `Ctrl+C` to stop tailing (the bot keeps running). Then run `/status` in your Discord channel to confirm — it should reply "no entry for today yet".

The systemd service is configured with `Restart=on-failure`, so if the bot crashes it'll auto-restart within 10 seconds. It also auto-starts on VM reboot.

### Updating the bot later

When you push code changes to GitHub, just SSH in and run:

```bash
cd ~/discord-med-bot
./update.sh
```

That pulls latest, refreshes Python deps, and restarts the service.

### Operational notes

- **Logs:** `journalctl -u med-bot -f` (live tail) or `journalctl -u med-bot -n 200` (last 200 lines)
- **Stop / start / status:** `sudo systemctl stop|start|restart|status med-bot`
- **OS updates:** `sudo apt update && sudo apt upgrade -y && sudo reboot` once a month or so. The bot auto-restarts after reboot.
- **VM reclamation risk:** Oracle has occasionally reclaimed Always Free A1 instances during capacity crunches. `uk-london-1` is generally OK in 2025+. If it ever happens you'd get an email and the VM stops; you'd recreate it (15 min) and restore from your weekly DB backup DM.

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

If you have to redeploy (VM reclaimed, switching providers, etc.):

1. Provision a new VM and run `oracle-setup.sh` again — but **don't start the service yet**.
2. Copy your most recent `med_bot_backup_<date>.db` (from your Discord DMs) onto the VM at `~/discord-med-bot/med_bot.db`. From your local machine: `scp -i <key> med_bot_backup_<date>.db ubuntu@<new-ip>:~/discord-med-bot/med_bot.db`
3. Fill in `.env` and `sudo systemctl start med-bot`. The bot picks up where it left off — `/month` will show all your old stickers.

## Customizing the schedule

The reminder hours and cutoff live in `config.py`:

```python
REMINDER_HOURS = (11, 15, 19, 23)
MISSED_CUTOFF_HOUR = 2
```

Change those constants and restart the bot. The scheduler uses `Europe/London` so DST is handled for you.

## Project layout

```
bot.py             # discord client, events, slash commands
scheduler.py       # APScheduler cron jobs + heartbeat / backup / weekly summary
storage.py         # SQLite log + queries
chart.py           # Pillow sticker chart + procedural sticker drawing
config.py          # env loader
oracle-setup.sh    # one-shot Oracle Cloud VM setup
update.sh          # pull + restart on the deployed VM
assets/stickers/   # generated on first run
```
