#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

exec .venv/bin/python bot.py
