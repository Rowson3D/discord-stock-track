"""Persistent message components. Custom IDs encode the listing, so buttons keep working
after restarts (discord.py DynamicItem)."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from tracker.queries import toggle_watch

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
        async with bot.ctx.sessions() as session, session.begin():
            watching = await toggle_watch(session, str(interaction.user.id), self.listing_id)
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
