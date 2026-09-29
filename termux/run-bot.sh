#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "$0")"
REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
cd "$REPO_DIR"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

exec .venv/bin/python bot.py