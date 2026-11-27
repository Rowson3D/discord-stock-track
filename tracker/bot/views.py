"""Persistent message components. Custom IDs encode the listing, so buttons keep working
after restarts (discord.py DynamicItem)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

from tracker.bot.embeds import item_name, place_name, status_text, unreadable
from tracker.db.models import Watch
from tracker.domain import StockStatus, format_cents, region_tag
from tracker.guilds import config_for
from tracker.queries import toggle_watch
from tracker.watching import ALERT_KEYS, WatchRow, parse_kinds, parse_regions
from tracker.watchtest import WatchTest

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
    """Under a public alert: Open (the store), Watch (toggle), Details (opens it privately)."""
    if listing_id is None and not url:
        return None
    view = discord.ui.View(timeout=None)
    if url:
        view.add_item(discord.ui.Button(label="Open", url=url, emoji="🛒"))
    if listing_id is not None:
        from tracker.bot.screens import button  # Local import: screens imports this module.

        view.add_item(WatchButton(listing_id))
        view.add_item(button("Details", "detail", str(listing_id), row=0))
    return view


# ── text for the Watches screen and watch tests ──

_WATCHES_SHOWN = 25  # Discord's select-menu limit.


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def watch_line(row: WatchRow, now: datetime) -> str:
    listing, watch = row.listing, row.watch
    snatch = "⚡ " if getattr(listing, "snatch", False) else ""
    parts = [
        f"{snatch}**{item_name(row.variant.sku, row.product.name)}**",
        place_name(listing.source, listing.region),
    ]
    if problem := unreadable(listing):
        parts.append(problem)
    else:
        parts.append(status_text(StockStatus(listing.status)))
        parts.append(format_cents(listing.price_cents, listing.currency))
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


def everything_text(subscription: Watch | None, shown: list[str] | None = None) -> str:
    """How 'ping me for everything' is set, in a few words."""
    if subscription is None:
        return "off"
    if shown is not None and len(shown) == 1:
        return "on"
    regions = parse_regions(subscription.regions)
    if regions is None:
        return "every region"
    return ", ".join(r.upper() for r in sorted(regions)) + " only"


def watches_embed(
    rows: list[WatchRow],
    now: datetime,
    subscription: Watch | None = None,
    shown: list[str] | None = None,
) -> discord.Embed:
    lines = [watch_line(r, now) for r in rows[:_WATCHES_SHOWN]]
    if len(rows) > _WATCHES_SHOWN:
        lines.append(f"…and {len(rows) - _WATCHES_SHOWN} more (all of them are in the panel)")
    if not rows:
        lines.append(
            "Nothing yet. Use **Find a product** on Home (or `/stock`), then pick the stores "
            "to watch."
        )
    lines += ["", f"**Ping me for every important alert:** {everything_text(subscription, shown)}"]
    embed = discord.Embed(
        title=f"Your watches ({len(rows)})", description="\n".join(lines)[:4000], color=0x5865F2
    )
    if rows:
        embed.set_footer(text="Open a watch to change its stores, target price, or mute it.")
    return embed


def coverage_line(test: WatchTest, index: int) -> str:
    item = test.coverage[index]
    where = f"<#{item.channel_id}>" if item.channel_id else ""
    if item.pings:
        line = f"✅ **{item.label}**: pings you in {where}"
    elif item.posted:
        line = f"➖ **{item.label}**: posts in {where}, no ping"
    else:
        line = f"🚫 **{item.label}**: not posted"
    if item.sms:
        line += " · 📱 SMS"
    return line


def watch_test_embed(test: WatchTest, link: str | None, error: str | None) -> discord.Embed:
    selected = test.selected
    if link:
        head = f"🧪 Sent a sample **{selected.label}** alert: {link}"
    elif error:
        head = f"🧪 Couldn't send the sample **{selected.label}** alert: {error}"
    else:
        head = f"🧪 Sample **{selected.label}** alert"
    lines = [head, "", f"**Why:** {selected.reason}", "", "**What this watch alerts you about**"]
    lines += [coverage_line(test, i) for i in range(len(test.coverage))]
    if any(item.sms for item in test.coverage):
        lines += ["", "📱 SMS goes out for these types too (not sent for tests)."]
    embed = discord.Embed(
        title=f"Test: {test.sku}" + (f" · {tag}" if (tag := region_tag(test.region)) else ""),
        description="\n".join(lines)[:4000],
        color=0x57F287 if selected.pings else 0xFEE75C,
    )
    embed.set_footer(
        text="Rules and 'ping me for everything' can also ping you; this checks the watch alone."
    )
    return embed
