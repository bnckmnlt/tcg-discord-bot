#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Checking working tree"
if [ -n "$(git status --porcelain)" ]; then
  echo "Your working tree has uncommitted changes."
  echo "Commit/stash them before updating so git pull cannot overwrite your phone edits."
  git status --short
  exit 1
fi

echo "==> Pulling latest bot code"
git pull --ff-only

echo "==> Updating Python dependencies"
.venv/bin/python -m pip install -r requirements.txt

echo "==> Restarting bot"
sv restart tcg-discord-bot

echo "==> Current service status"
sv status tcg-discord-bot
