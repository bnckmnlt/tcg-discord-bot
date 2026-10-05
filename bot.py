from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import discord
import requests
from dotenv import load_dotenv
from discord import app_commands

load_dotenv()

LOG = logging.getLogger("tcg-discord-bot")

GITHUB_API = "https://api.github.com"
WATCHLIST_PATH = "data/watchlist.json"


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass
class WatchDraft:
    url: str
    name: str
    set_name: str
    target_price: float
    max_price: float | None
    quantity_needed: int
    min_condition: str
    language: str
    printing: str


class GitHubWatchlist:
    def __init__(self) -> None:
        self.token = required_env("GITHUB_TOKEN")
        self.owner = required_env("GITHUB_OWNER")
        self.repo = required_env("GITHUB_REPO")
        self.branch = os.getenv("GITHUB_BRANCH", "main")
        self.session = requests.Session()
        self.session.headers.update(self._headers())
        self._write_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{WATCHLIST_PATH}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def load(self) -> tuple[dict, str]:
        response = self.session.get(
            self.url,
            params={"ref": self.branch},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        return json.loads(content), payload["sha"]

    def add(self, card: dict) -> int:
        # GitHub's Contents API uses the file SHA as an optimistic concurrency
        # check. Serialize writes locally and retry a stale-SHA conflict by
        # re-reading the file before attempting the update again.
        with self._write_lock:
            for attempt in range(2):
                watchlist, sha = self.load()
                items = watchlist.setdefault("watchlist", [])
                items.append(card)
                content = json.dumps(watchlist, indent=2, ensure_ascii=False) + "\n"
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                response = self.session.put(
                    self.url,
                    json={
                        "message": f"Add watchlist card: {card['name']}",
                        "content": encoded,
                        "sha": sha,
                        "branch": self.branch,
                    },
                    timeout=20,
                )
                if response.status_code != 409 or attempt == 1:
                    response.raise_for_status()
                    return len(items)

                LOG.warning("GitHub watchlist changed concurrently; retrying update")
            raise RuntimeError("GitHub watchlist update failed after retry")

    def list(self) -> list[dict]:
        watchlist, _ = self.load()
        return watchlist.get("watchlist", [])


def parse_tcgplayer_url(url: str) -> dict[str, str]:
    # Accept normal TCGPlayer product URLs, including their query parameters
    # (Printing, Condition, Language, page, etc.).
    url = url.strip().strip("<>")
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in {"tcgplayer.com", "www.tcgplayer.com"}:
        raise ValueError("Please provide a valid TCGPlayer URL.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0].lower() != "product" or not parts[1].isdigit():
        raise ValueError("That does not look like a TCGPlayer product URL.")

    slug = parts[2]
    query = parse_qs(parsed.query)
    language = query.get("Language", ["English"])[0]
    condition = query.get("Condition", ["Near Mint"])[0]
    printing = query.get("Printing", [""])[0]

    # TCGPlayer slugs commonly contain the set and card name. We keep this
    # conservative: the user can review/edit the extracted values before saving.
    words = slug.split("-")
    name = words[-1].replace("%20", " ").title() if words else "Unknown"
    set_name = " ".join(words[:-1]).replace("%20", " ").replace("-", " ").title()
    return {
        "name": name,
        "set_name": set_name,
        "language": language,
        "condition": condition,
        "printing": printing,
    }


class AddCardModal(discord.ui.Modal, title="Add TCGPlayer Card"):
    url = discord.ui.TextInput(label="TCGPlayer URL", placeholder="https://www.tcgplayer.com/product/...", required=True)
    name = discord.ui.TextInput(label="Card name", required=True)
    set_name = discord.ui.TextInput(label="Set name", required=True)
    target = discord.ui.TextInput(label="Target price (USD)", placeholder="6.00", required=True)
    maximum = discord.ui.TextInput(label="Max landed price (USD)", placeholder="8.00 or leave blank", required=False)

    def __init__(self, bot: "TCGDiscordBot") -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            target = float(self.target.value)
            maximum = float(self.maximum.value) if self.maximum.value.strip() else None
            if target < 0 or (maximum is not None and maximum < 0):
                raise ValueError("Prices cannot be negative.")
            if maximum is not None and maximum < target:
                raise ValueError("Max landed price cannot be lower than the target price.")
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        url = self.url.value.strip()
        try:
            parse_tcgplayer_url(url)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        draft = WatchDraft(
            url=url,
            name=self.name.value.strip(),
            set_name=self.set_name.value.strip(),
            target_price=target,
            max_price=maximum,
            quantity_needed=1,
            min_condition="Near Mint",
            language="English",
            printing="",
        )
        await interaction.response.send_message(
            "Choose the remaining watch settings:",
            view=WatchSettingsView(self.bot, draft),
            ephemeral=True,
        )


class WatchSettingsView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", draft: WatchDraft) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft

    @discord.ui.select(
        placeholder="Minimum condition",
        options=[
            discord.SelectOption(label="Near Mint", value="Near Mint"),
            discord.SelectOption(label="Lightly Played", value="Lightly Played"),
            discord.SelectOption(label="Moderately Played", value="Moderately Played"),
            discord.SelectOption(label="Any", value=""),
        ],
    )
    async def condition(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft.min_condition = select.values[0]
        await interaction.response.edit_message(content="Condition saved. Now choose quantity.", view=QuantityView(self.bot, self.draft))


class QuantityView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", draft: WatchDraft) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft

    @discord.ui.select(
        placeholder="Quantity needed",
        options=[discord.SelectOption(label=str(i), value=str(i)) for i in range(1, 6)],
    )
    async def quantity(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft.quantity_needed = int(select.values[0])
        await interaction.response.edit_message(content="Quantity saved. Review your watch:", view=ConfirmView(self.bot, self.draft))


class ConfirmView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", draft: WatchDraft) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        card = {
            "name": self.draft.name,
            "set_name": self.draft.set_name,
            "url": self.draft.url,
            "target_price": self.draft.target_price,
            "quantity_needed": self.draft.quantity_needed,
            "min_condition": self.draft.min_condition,
            "language": self.draft.language,
            "printing": self.draft.printing,
            "price_drop_percent": 10,
            "deal_score_threshold": 70,
            "enabled": True,
            "tags": ["discord"],
        }
        if self.draft.max_price is not None:
            card["max_price"] = self.draft.max_price

        try:
            count = await self.bot.github_add(card)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            LOG.exception("GitHub update failed")
            await interaction.response.edit_message(
                content=f"❌ GitHub update failed: {exc}",
                view=None,
            )
            return

        await interaction.response.edit_message(
            content=f"✅ Added **{self.draft.name}** to the watchlist.\nWatchlist now has **{count}** cards.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class TCGDiscordBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.github = GitHubWatchlist()

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOG.info("Synced commands to guild %s", guild_id)
        else:
            await self.tree.sync()
            LOG.info("Synced global commands")

    async def github_add(self, card: dict) -> int:
        # requests is synchronous; keep network I/O off Discord's event loop.
        return await asyncio.to_thread(self.github.add, card)

    async def github_list(self) -> list[dict]:
        return await asyncio.to_thread(self.github.list)


bot = TCGDiscordBot()


def authorized(interaction: discord.Interaction) -> bool:
    allowed = os.getenv("DISCORD_ALLOWED_USER_ID")
    return not allowed or str(interaction.user.id) == allowed


@bot.tree.command(name="add-card", description="Start an interactive TCGPlayer watchlist setup")
async def add_card(interaction: discord.Interaction) -> None:
    if not authorized(interaction):
        await interaction.response.send_message("❌ You are not authorized to manage the watchlist.", ephemeral=True)
        return
    await interaction.response.send_modal(AddCardModal(bot))


@bot.tree.command(name="list-cards", description="Show cards currently on the TCGPlayer watchlist")
async def list_cards(interaction: discord.Interaction) -> None:
    if not authorized(interaction):
        await interaction.response.send_message("❌ You are not authorized to manage the watchlist.", ephemeral=True)
        return
    try:
        cards = await bot.github_list()
    except requests.RequestException as exc:
        await interaction.response.send_message(f"❌ GitHub read failed: {exc}", ephemeral=True)
        return

    if not cards:
        await interaction.response.send_message("The watchlist is empty.", ephemeral=True)
        return

    lines = []
    for index, card in enumerate(cards, 1):
        status = "🟢" if card.get("enabled", True) else "⚪"
        maximum = card.get("max_price")
        max_display = "—" if maximum in (None, "", "—") else f"${float(maximum):.2f}"
        lines.append(
            f"{status} **{index}. {card.get('name', 'Unknown')}** — {card.get('set_name', 'Unknown')}\n"
            f"Target ${float(card.get('target_price', 0)):.2f} | Max {max_display} | Qty {card.get('quantity_needed', 1)} | {card.get('min_condition', 'Any')}"
        )

    message = "\n\n".join(lines)
    # Discord limits a normal message to 2000 characters. Keep the command
    # useful even when the watchlist grows beyond that size.
    if len(message) <= 2000:
        await interaction.response.send_message(message, ephemeral=True)
        return

    chunks: list[str] = []
    current = ""
    for block in lines:
        candidate = f"{current}\n\n{block}".strip()
        if len(candidate) > 1900 and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)

    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    LOG.exception("Unhandled application command error", exc_info=error)
    message = "❌ Something went wrong while processing that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.event
async def on_ready() -> None:
    LOG.info("Logged in as %s", bot.user)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot.run(required_env("DISCORD_BOT_TOKEN"), log_handler=None)
