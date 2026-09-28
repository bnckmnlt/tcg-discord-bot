#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
SERVICE_DIR="$PREFIX/var/service/tcg-discord-bot"

echo "==> Installing Termux packages"
pkg update
pkg install -y python git termux-services

echo "==> Creating Python virtual environment"
cd "$REPO_DIR"
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created $REPO_DIR/.env. Fill in your Discord/GitHub credentials before starting the service."
fi

echo "==> Installing service"
mkdir -p "$SERVICE_DIR/log"
rm -f "$SERVICE_DIR/run" "$SERVICE_DIR/log/run"
ln -s "$REPO_DIR/termux/run-bot.sh" "$SERVICE_DIR/run"
chmod +x "$REPO_DIR/termux/run-bot.sh"

cat > "$SERVICE_DIR/log/run" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
exec svlogd "$HOME/.termux/log/tcg-discord-bot"
EOF
chmod +x "$SERVICE_DIR/log/run"
mkdir -p "$HOME/.termux/log/tcg-discord-bot"

echo
echo "Installation complete."
echo "1. Edit: $REPO_DIR/.env"
echo "2. Close and reopen Termux once so termux-services starts its service manager"
echo "3. Start bot: sv up tcg-discord-bot"
echo "4. Check status: sv status tcg-discord-bot"
echo "5. View live logs: tail -f $HOME/.termux/log/tcg-discord-bot/current"
