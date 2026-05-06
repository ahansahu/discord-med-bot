#!/usr/bin/env bash
# Pulls latest code, reinstalls Python deps if needed, and restarts the service.
# Run from the project directory: ./update.sh

set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-med-bot}"

echo "==> pulling latest"
git pull --ff-only

echo "==> updating Python deps"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> restarting ${SERVICE_NAME}"
sudo systemctl restart "$SERVICE_NAME"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager || true

echo
echo "tail logs: journalctl -u ${SERVICE_NAME} -f"
