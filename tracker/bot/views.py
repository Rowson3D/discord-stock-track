"""Persistent message components. Custom IDs encode the listing, so buttons keep working
after restarts (discord.py DynamicItem)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord

from tracker.bot.embeds import source_name, status_text
from tracker.domain import StockStatus, format_cents
from tracker.guilds import config_for
from tracker.queries import toggle_watch
from tracker.watching import (
    ALERT_KEYS,
    WatchRow,
    mute_watches,
    parse_kinds,
    remove_watch_ids,
    user_watch_rows,
)

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot

logger = logging.getLogger(__name__)


class WatchButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"tracker:watch:(?P<listing_id>\d+)",
):
    def __init__(self, listing_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Watch",
                emoji="🔔",
                style=discord.ButtonStyle.secondary,
                custom_id=f"tracker:watch:{listing_id}",
            )
        )
        self.listing_id = listing_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> WatchButton:
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        async with bot.ctx.sessions() as session, session.begin():
            config = (
                await config_for(session, bot.ctx.settings.discord, guild_id) if guild_id else None
            )
            if config is None or not config.approved:
                await interaction.response.send_message(
                    "This server isn't approved to use the tracker.", ephemeral=True
                )
                return
            watching = await toggle_watch(
                session, str(interaction.user.id), self.listing_id, guild_id
            )
        message = (
            "🔔 Watching. You'll be pinged when this is back in stock, goes Early Access, or "
            "drops in price."
            if watching
            else "🔕 Stopped watching this listing."
        )
        await interaction.response.send_message(message, ephemeral=True)


def alert_view(listing_id: int | None, url: str | None) -> discord.ui.View | None:
    if listing_id is None and not url:
        return None
    view = discord.ui.View(timeout=None)
    if url:
        view.add_item(discord.ui.Button(label="Open", url=url, emoji="🛒"))
    if listing_id is not None:
        view.add_item(WatchButton(listing_id))
    return view


# ── /watches management (ephemeral; only the person who ran it can use it) ──

MUTE_HOURS = 24
_WATCHES_SHOWN = 25  # Discord's select-menu limit.


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def watch_line(row: WatchRow, now: datetime) -> str:
    listing, watch = row.listing, row.watch
    parts = [
        f"**{row.variant.sku}**",
        f"{source_name(listing.source)} {listing.region.upper()}",
        status_text(StockStatus(listing.status)),
        format_cents(listing.price_cents, listing.currency),
    ]
    if watch.max_price_cents is not None:
        parts.append(f"ping ≤ {format_cents(watch.max_price_cents, listing.currency)}")
    chosen = parse_kinds(watch.kinds)
    if chosen is not None:
        parts.append("alerts: " + (", ".join(ALERT_KEYS[k] for k in sorted(chosen)) or "none"))
    if watch.muted_until is not None and _aware(watch.muted_until) > now:
        parts.append(f"🔕 muted until <t:{int(_aware(watch.muted_until).timestamp())}:R>")
    if listing.delisted_at:
        parts.append("delisted")
    return " · ".join(parts)


def watches_embed(rows: list[WatchRow], now: datetime) -> discord.Embed:
    lines = [watch_line(r, now) for r in rows]
    text = "\n".join(lines[:_WATCHES_SHOWN])
    if len(rows) > _WATCHES_SHOWN:
        text += f"\n…and {len(rows) - _WATCHES_SHOWN} more (manage all of them in the owner panel)"
    embed = discord.Embed(
        title=f"🔔 Your watches ({len(rows)})", description=text[:4000], color=0x5865F2
    )
    embed.set_footer(text="Pick watches below, then Remove, Mute 24h, or Unmute.")
    return embed


class WatchManager(discord.ui.View):
    """Select some of your watches, then remove, mute, or unmute them."""

    def __init__(self, bot: TrackerBot, user_id: str, rows: list[WatchRow]) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = user_id
        self.selected: list[int] = []
        shown = rows[:_WATCHES_SHOWN]
        self.picker: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Choose watches…",
            min_values=1,
            max_values=max(1, len(shown)),
            options=[
                discord.SelectOption(
                    label=f"{r.variant.sku} · {r.listing.region.upper()}"[:100],
                    value=str(r.watch.id),
                    description=(r.product.name or "")[:100] or None,
                )
                for r in shown
            ],
        )
        self.picker.callback = self._picked  # type: ignore[method-assign]
        self.add_item(self.picker)
        for label, style, action in (
            ("Remove", discord.ButtonStyle.danger, "remove"),
            (f"Mute {MUTE_HOURS}h", discord.ButtonStyle.secondary, "mute"),
            ("Unmute", discord.ButtonStyle.secondary, "unmute"),
        ):
            button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style)
            button.callback = self._handler(action)  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Use /watches to manage your own watches.", ephemeral=True
            )
            return False
        return True

    async def _picked(self, interaction: discord.Interaction[Any]) -> None:
        self.selected = [int(value) for value in self.picker.values]
        await interaction.response.defer()

    def _handler(self, action: str) -> Any:
        async def handle(interaction: discord.Interaction[Any]) -> None:
            await self.apply(interaction, action)

        return handle

    async def apply(self, interaction: discord.Interaction[Any], action: str) -> None:
        if not self.selected:
            await interaction.response.send_message(
                "Pick one or more watches first.", ephemeral=True
            )
            return
        now = datetime.now(UTC)
        async with self.bot.ctx.sessions() as session, session.begin():
            if action == "remove":
                count = await remove_watch_ids(session, self.user_id, self.selected)
            else:
                until = now + timedelta(hours=MUTE_HOURS) if action == "mute" else None
                count = await mute_watches(session, self.user_id, self.selected, until)
            rows = await user_watch_rows(session, self.user_id)
        done = {"remove": "Removed", "mute": f"Muted for {MUTE_HOURS}h", "unmute": "Unmuted"}
        if not rows:
            await interaction.response.edit_message(
                content=f"{done[action]} {count}. You aren't watching anything now.",
                embed=None,
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"✅ {done[action]} {count} watch(es).",
            embed=watches_embed(rows, now),
            view=WatchManager(self.bot, self.user_id, rows),
        )
