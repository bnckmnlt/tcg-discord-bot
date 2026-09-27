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
