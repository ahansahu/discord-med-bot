#!/usr/bin/env bash
# One-shot setup for discord-med-bot on a fresh Oracle Cloud Always Free Ubuntu VM.
# Idempotent — safe to re-run.
#
# Usage (after SSH'ing into your VM):
#   curl -O https://raw.githubusercontent.com/ahansahu/discord-med-bot/master/oracle-setup.sh
#   chmod +x oracle-setup.sh
#   ./oracle-setup.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ahansahu/discord-med-bot.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/discord-med-bot}"
SERVICE_NAME="med-bot"
SERVICE_USER="$(whoami)"

echo "==> discord-med-bot Oracle Cloud setup"
echo "    user        : $SERVICE_USER"
echo "    install dir : $INSTALL_DIR"
echo "    service     : $SERVICE_NAME"
echo

# 1. Install system dependencies
echo "==> installing system dependencies"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git build-essential

# 2. Clone (or pull) the repo
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> repo already cloned, pulling latest"
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "==> cloning $REPO_URL"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# 3. Create venv and install Python deps
if [ ! -d ".venv" ]; then
    echo "==> creating Python venv"
    python3 -m venv .venv
fi
echo "==> installing Python dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# 4. Create .env from template if missing
NEED_ENV_EDIT=0
if [ ! -f ".env" ]; then
    echo "==> creating .env from template (you will need to fill it in)"
    cp .env.example .env
    chmod 600 .env
    NEED_ENV_EDIT=1
fi

# 5. Create or refresh the systemd unit
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
echo "==> writing systemd unit ${SERVICE_PATH}"
sudo tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=Discord medicine bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/.venv/bin/python bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

echo
echo "==> setup complete"
echo

if [ "$NEED_ENV_EDIT" -eq 1 ]; then
    cat <<MSG
Next steps:

  1. Edit your env vars:
       nano ${INSTALL_DIR}/.env

     Fill in:
       DISCORD_TOKEN, GUILD_ID, CHANNEL_ID, TARGET_USER_ID
     (TIMEZONE is already set to Europe/London.)

  2. Start the bot:
       sudo systemctl start ${SERVICE_NAME}

  3. Tail the logs to verify it came up clean:
       journalctl -u ${SERVICE_NAME} -f

You should see:
  logged in as <your-bot-name> (id=...)
  synced 5 guild commands
  scheduler started; jobs=['reminder_11', ...]

To update later: cd ${INSTALL_DIR} && ./update.sh
MSG
else
    echo ".env already exists — restarting service"
    sudo systemctl restart "$SERVICE_NAME"
    sleep 2
    sudo systemctl status "$SERVICE_NAME" --no-pager || true
    echo
    echo "Tail logs with: journalctl -u ${SERVICE_NAME} -f"
fi
