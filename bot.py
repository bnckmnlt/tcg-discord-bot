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

    def _save(self, watchlist: dict, sha: str, message: str) -> None:
        content = json.dumps(watchlist, indent=2, ensure_ascii=False) + "\n"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        response = self.session.put(
            self.url,
            json={
                "message": message,
                "content": encoded,
                "sha": sha,
                "branch": self.branch,
            },
            timeout=20,
        )
        if response.status_code == 403:
            try:
                details = response.json().get("message", response.text)
            except ValueError:
                details = response.text
            raise RuntimeError(f"GitHub permission denied: {details}")
        response.raise_for_status()

    def add(self, card: dict) -> int:
        with self._write_lock:
            for attempt in range(2):
                watchlist, sha = self.load()
                items = watchlist.setdefault("watchlist", [])
                items.append(card)
                try:
                    self._save(watchlist, sha, f"Add watchlist card: {card['name']}")
                    return len(items)
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 409 or attempt == 1:
                        raise
                    LOG.warning("GitHub watchlist changed concurrently; retrying update")
            raise RuntimeError("GitHub watchlist update failed after retry")

    def remove(self, index: int) -> tuple[dict, int]:
        with self._write_lock:
            for attempt in range(2):
                watchlist, sha = self.load()
                items = watchlist.setdefault("watchlist", [])
                if index < 0 or index >= len(items):
                    raise IndexError("That watchlist card no longer exists.")
                removed = items.pop(index)
                try:
                    self._save(watchlist, sha, f"Remove watchlist card: {removed.get('name', 'Unknown')}")
                    return removed, len(items)
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 409 or attempt == 1:
                        raise
                    LOG.warning("GitHub watchlist changed concurrently; retrying update")
            raise RuntimeError("GitHub watchlist update failed after retry")

    def update(self, index: int, card: dict) -> tuple[dict, int]:
        with self._write_lock:
            for attempt in range(2):
                watchlist, sha = self.load()
                items = watchlist.setdefault("watchlist", [])
                if index < 0 or index >= len(items):
                    raise IndexError("That watchlist card no longer exists.")
                previous = items[index]
                items[index] = card
                try:
                    self._save(watchlist, sha, f"Edit watchlist card: {card.get('name', 'Unknown')}")
                    return previous, len(items)
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 409 or attempt == 1:
                        raise
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

    # The product ID is the only required path component. TCGPlayer can
    # change the slug format, so don't make the validator depend on it.
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "product" or not parts[1].isdigit():
        raise ValueError("That does not look like a TCGPlayer product URL.")

    slug = parts[2] if len(parts) >= 3 else parts[1]
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
            "Choose the minimum condition:",
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
            discord.SelectOption(label="Any", value="Any"),
        ],
    )
    async def condition(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft.min_condition = "" if select.values[0] == "Any" else select.values[0]
        self.draft.quantity_needed = 1
        await interaction.response.edit_message(content="Condition saved. Review your watch:", view=ConfirmView(self.bot, self.draft))


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


class EditCardModal(discord.ui.Modal, title="Edit TCGPlayer Card"):
    name = discord.ui.TextInput(label="Card name", required=True)
    set_name = discord.ui.TextInput(label="Set name", required=True)
    target = discord.ui.TextInput(label="Target price (USD)", required=True)
    maximum = discord.ui.TextInput(label="Max landed price (USD)", placeholder="Leave blank for no limit", required=False)
    url = discord.ui.TextInput(label="TCGPlayer URL", required=True)

    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict) -> None:
        super().__init__()
        self.bot = bot
        self.index = index
        self.original = card
        self.name.default = str(card.get("name", ""))
        self.set_name.default = str(card.get("set_name", ""))
        self.target.default = str(card.get("target_price", ""))
        self.maximum.default = "" if card.get("max_price") in (None, "") else str(card.get("max_price"))
        self.url.default = str(card.get("url", ""))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            target = float(self.target.value)
            maximum = float(self.maximum.value) if self.maximum.value.strip() else None
            if target < 0 or (maximum is not None and maximum < 0):
                raise ValueError("Prices cannot be negative.")
            if maximum is not None and maximum < target:
                raise ValueError("Max landed price cannot be lower than the target price.")
            parse_tcgplayer_url(self.url.value.strip())
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        draft = dict(self.original)
        draft.update({
            "name": self.name.value.strip(),
            "set_name": self.set_name.value.strip(),
            "target_price": target,
            "url": self.url.value.strip(),
        })
        if maximum is None:
            draft.pop("max_price", None)
        else:
            draft["max_price"] = maximum

        await interaction.response.send_message(
            "Choose the minimum condition:",
            view=EditConditionView(self.bot, self.index, draft),
            ephemeral=True,
        )


class EditConditionView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.index = index
        self.card = card

    @discord.ui.select(
        placeholder="Minimum condition",
        options=[
            discord.SelectOption(label="Near Mint", value="Near Mint"),
            discord.SelectOption(label="Lightly Played", value="Lightly Played"),
            discord.SelectOption(label="Moderately Played", value="Moderately Played"),
            discord.SelectOption(label="Any / no minimum", value="Any"),
        ],
    )
    async def condition(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.card["min_condition"] = "" if select.values[0] == "Any" else select.values[0]
        await interaction.response.send_modal(
            EditAdvancedModal(self.bot, self.index, self.card)
        )


class EditAdvancedModal(discord.ui.Modal, title="Edit Card — Additional Settings"):
    quantity = discord.ui.TextInput(
        label="Quantity needed",
        placeholder="1",
        required=False,
    )
    language = discord.ui.TextInput(
        label="Language",
        placeholder="English (blank = default)",
        required=False,
    )
    printing = discord.ui.TextInput(
        label="Printing",
        placeholder="Holofoil, Reverse Holo, etc. (optional)",
        required=False,
    )
    price_drop = discord.ui.TextInput(
        label="Price-drop alert %",
        placeholder="10 (blank = use default)",
        required=False,
    )
    score_threshold = discord.ui.TextInput(
        label="Deal-score threshold",
        placeholder="70 (blank = use default)",
        required=False,
    )

    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict) -> None:
        super().__init__()
        self.bot = bot
        self.index = index
        self.card = card
        self.quantity.default = str(card.get("quantity_needed", ""))
        self.language.default = str(card.get("language", ""))
        self.printing.default = str(card.get("printing", ""))
        self.price_drop.default = str(card.get("price_drop_percent", ""))
        self.score_threshold.default = str(card.get("deal_score_threshold", ""))

    @staticmethod
    def _optional_number(value: str, label: str, minimum: float = 0) -> float | None:
        value = value.strip()
        if not value:
            return None
        parsed = float(value)
        if parsed < minimum:
            raise ValueError(f"{label} must be at least {minimum:g}.")
        return parsed

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            quantity = self.quantity.value.strip()
            if quantity:
                quantity_value = int(quantity)
                if quantity_value < 1:
                    raise ValueError("Quantity needed must be at least 1.")
                self.card["quantity_needed"] = quantity_value
            else:
                self.card.pop("quantity_needed", None)

            language = self.language.value.strip()
            if language:
                self.card["language"] = language
            else:
                self.card.pop("language", None)

            printing = self.printing.value.strip()
            if printing:
                self.card["printing"] = printing
            else:
                self.card.pop("printing", None)

            price_drop = self._optional_number(self.price_drop.value, "Price-drop alert %")
            if price_drop is None:
                self.card.pop("price_drop_percent", None)
            else:
                self.card["price_drop_percent"] = price_drop

            score_threshold = self._optional_number(self.score_threshold.value, "Deal-score threshold")
            if score_threshold is None:
                self.card.pop("deal_score_threshold", None)
            else:
                self.card["deal_score_threshold"] = score_threshold
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await interaction.response.send_modal(
            EditTriggerModal(self.bot, self.index, self.card)
        )


class EditTriggerModal(discord.ui.Modal, title="Edit Card — Deal Triggers"):
    min_savings = discord.ui.TextInput(
        label="Minimum savings (USD)",
        placeholder="Blank = disabled",
        required=False,
    )
    min_discount = discord.ui.TextInput(
        label="Minimum discount %",
        placeholder="Blank = disabled",
        required=False,
    )
    history_days = discord.ui.TextInput(
        label="History window (days)",
        placeholder="7 (blank = use default)",
        required=False,
    )
    history_discount = discord.ui.TextInput(
        label="Historical discount %",
        placeholder="Blank = disabled",
        required=False,
    )
    tags = discord.ui.TextInput(
        label="Tags",
        placeholder="discord, vintage (comma-separated, optional)",
        required=False,
    )

    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict) -> None:
        super().__init__()
        self.bot = bot
        self.index = index
        self.card = card
        self.min_savings.default = str(card.get("min_savings", ""))
        self.min_discount.default = str(card.get("min_discount_percent", ""))
        self.history_days.default = str(card.get("history_days", ""))
        self.history_discount.default = str(card.get("history_discount_percent", ""))
        self.tags.default = ", ".join(str(tag) for tag in card.get("tags", []))

    @staticmethod
    def _optional_number(value: str, label: str, minimum: float = 0) -> float | None:
        value = value.strip()
        if not value:
            return None
        parsed = float(value)
        if parsed < minimum:
            raise ValueError(f"{label} must be at least {minimum:g}.")
        return parsed

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            fields = (
                ("min_savings", self.min_savings.value, "Minimum savings"),
                ("min_discount_percent", self.min_discount.value, "Minimum discount %"),
                ("history_discount_percent", self.history_discount.value, "Historical discount %"),
            )
            for key, raw, label in fields:
                value = self._optional_number(raw, label)
                if value is None:
                    self.card.pop(key, None)
                else:
                    self.card[key] = value

            history_days = self.history_days.value.strip()
            if history_days:
                history_days_value = int(history_days)
                if history_days_value < 1:
                    raise ValueError("History window must be at least 1 day.")
                self.card["history_days"] = history_days_value
            else:
                self.card.pop("history_days", None)

            tags = [tag.strip() for tag in self.tags.value.split(",") if tag.strip()]
            if tags:
                self.card["tags"] = tags
            else:
                self.card.pop("tags", None)
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await interaction.response.send_message(
            "All editable fields saved locally. Review the complete change before committing:",
            view=EditConfirmView(self.bot, self.index, self.card),
            ephemeral=True,
        )


class EditConfirmView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.index = index
        self.card = card

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            _, count = await self.bot.github_update(self.index, self.card)
        except (requests.RequestException, ValueError, RuntimeError, IndexError) as exc:
            LOG.exception("GitHub update failed")
            await interaction.response.edit_message(content=f"❌ GitHub update failed: {exc}", view=None)
            return

        await interaction.response.edit_message(
            content=f"✅ Updated **{self.card.get('name', 'Unknown')}**.\nWatchlist still has **{count}** cards.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class RemoveConfirmView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.index = index
        self.card = card

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            removed, count = await self.bot.github_remove(self.index)
        except (requests.RequestException, ValueError, RuntimeError, IndexError) as exc:
            LOG.exception("GitHub update failed")
            await interaction.response.edit_message(content=f"❌ GitHub update failed: {exc}", view=None)
            return

        await interaction.response.edit_message(
            content=f"✅ Removed **{removed.get('name', self.card.get('name', 'Unknown'))}**.\nWatchlist now has **{count}** cards.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class ToggleCardView(discord.ui.View):
    def __init__(self, bot: "TCGDiscordBot", index: int, card: dict, enabled: bool) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.index = index
        self.card = card
        self.enabled = enabled

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        updated = dict(self.card)
        updated["enabled"] = self.enabled
        try:
            _, count = await self.bot.github_update(self.index, updated)
        except (requests.RequestException, ValueError, RuntimeError, IndexError) as exc:
            LOG.exception("GitHub update failed")
            await interaction.response.edit_message(content=f"❌ GitHub update failed: {exc}", view=None)
            return

        state = "resumed" if self.enabled else "paused"
        await interaction.response.edit_message(
            content=f"✅ **{updated.get('name', 'Unknown')}** is now **{state}**.\nWatchlist still has **{count}** cards.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class CardPickerView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, bot: "TCGDiscordBot", cards: list[dict], mode: str) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.cards = cards
        self.mode = mode
        self.page = 0
        self._refresh()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.cards) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _refresh(self) -> None:
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        current = self.cards[start:start + self.PAGE_SIZE]
        options = []
        for offset, card in enumerate(current):
            index = start + offset
            label = str(card.get("name", "Unknown"))[:100]
            description = str(card.get("set_name", "Unknown"))[:100]
            options.append(discord.SelectOption(label=label or "Unknown", description=description or None, value=str(index)))

        select = discord.ui.Select(
            placeholder="Select a card",
            options=options,
        )
        select.callback = self._selected
        self.add_item(select)

        previous = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        previous.callback = self._previous
        self.add_item(previous)
        next_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= self.page_count - 1)
        next_button.callback = self._next
        self.add_item(next_button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _selected(self, interaction: discord.Interaction) -> None:
        select = interaction.data
        index = int(select["values"][0])
        card = self.cards[index]
        if self.mode == "edit":
            await interaction.response.send_modal(EditCardModal(self.bot, index, card))
        elif self.mode in {"pause", "resume"}:
            enabled = self.mode == "resume"
            await interaction.response.edit_message(
                content=f"{'Resume' if enabled else 'Pause'} **{card.get('name', 'Unknown')}** — {card.get('set_name', 'Unknown')}?",
                view=ToggleCardView(self.bot, index, card, enabled),
            )
        else:
            await interaction.response.edit_message(
                content=f"Remove **{card.get('name', 'Unknown')}** — {card.get('set_name', 'Unknown')}?",
                view=RemoveConfirmView(self.bot, index, card),
            )

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
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

    async def github_remove(self, index: int) -> tuple[dict, int]:
        return await asyncio.to_thread(self.github.remove, index)

    async def github_update(self, index: int, card: dict) -> tuple[dict, int]:
        return await asyncio.to_thread(self.github.update, index, card)


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


@bot.tree.command(name="remove-card", description="Remove a card from the TCGPlayer watchlist")
async def remove_card(interaction: discord.Interaction) -> None:
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

    await interaction.response.send_message(
        "Select the card to remove:",
        view=CardPickerView(bot, cards, "remove"),
        ephemeral=True,
    )


@bot.tree.command(name="edit-card", description="Edit a card on the TCGPlayer watchlist")
async def edit_card(interaction: discord.Interaction) -> None:
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

    await interaction.response.send_message(
        "Select the card to edit:",
        view=CardPickerView(bot, cards, "edit"),
        ephemeral=True,
    )


@bot.tree.command(name="pause-card", description="Pause alerts for a TCGPlayer watchlist card")
async def pause_card(interaction: discord.Interaction) -> None:
    await toggle_card(interaction, "pause")


@bot.tree.command(name="resume-card", description="Resume alerts for a TCGPlayer watchlist card")
async def resume_card(interaction: discord.Interaction) -> None:
    await toggle_card(interaction, "resume")


async def toggle_card(interaction: discord.Interaction, mode: str) -> None:
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

    action = "pause" if mode == "pause" else "resume"
    await interaction.response.send_message(
        f"Select the card to {action}:",
        view=CardPickerView(bot, cards, mode),
        ephemeral=True,
    )


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
