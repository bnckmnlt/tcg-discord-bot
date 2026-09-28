# TCG Discord Bot

A small Discord bot for managing the TCGPlayer Alert Bot watchlist.

## What it does

- `/add-card` opens an interactive setup flow.
- `/list-cards` reads the current watchlist from GitHub.
- Confirming a card updates `data/watchlist.json` in the `tcg-alert-bot` repository through the GitHub API.
- The existing GitHub Actions scraper then uses the updated watchlist on its next run.

## Railway configuration

Set these environment variables in Railway:

- `DISCORD_BOT_TOKEN` — Discord bot token.
- `DISCORD_GUILD_ID` — optional; setting it makes slash commands sync immediately to one server.
- `DISCORD_ALLOWED_USER_ID` — recommended; restricts watchlist commands to your Discord user.
- `GITHUB_TOKEN` — a GitHub fine-grained token with **Contents: Read and write** access to the `tcg-alert-bot` repository.
- `GITHUB_OWNER` — your GitHub username or organization.
- `GITHUB_REPO` — `tcg-alert-bot`.
- `GITHUB_BRANCH` — normally `main`.

Do not commit `.env` or any token to GitHub.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

The bot needs to be invited to the Discord server with the `bot` and `applications.commands` scopes.

## Run it on Android with Termux

The bot can run directly on your phone without Railway or Render. The recommended setup uses **Termux + termux-services**, so the Discord bot runs as a supervised background service and automatically restarts if the process exits.

### One-time setup

Clone the repository on your phone, then run:

```bash
cd ~/tcg-discord-bot
bash termux/install.sh
nano .env
```

Fill in the same credentials described in the Railway configuration section.

Then close and reopen Termux once, and start the service:

```bash
sv up tcg-discord-bot
sv status tcg-discord-bot
```

To see the bot logs:

```bash
tail -f ~/.termux/log/tcg-discord-bot/current
```

To stop it:

```bash
sv down tcg-discord-bot
```

### Updating the bot from your phone

If you edit the project elsewhere and push the changes to GitHub:

```bash
cd ~/tcg-discord-bot
bash termux/update.sh
```

That script:

1. Refuses to update if you have uncommitted phone-side changes.
2. Pulls the latest Git commit.
3. Updates Python dependencies.
4. Restarts the Discord bot.
5. Shows the service status.

If you want to edit the bot directly on your phone, use an Android editor that works with the repository, then commit and push normally.

### Important Android limitation

Termux is still subject to Android's background-process and battery-management behavior. For reliable operation:

- Exclude Termux from battery optimization.
- Keep the phone sufficiently charged.
- Do not force-stop Termux.
- `termux/run-bot.sh` uses `termux-wake-lock` when available to reduce sleep-related interruptions.

This is a phone-hosted service, not a replacement for a true always-on server. If Android kills Termux, the bot will be offline until Termux's service environment is running again.
