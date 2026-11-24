"""Persistent message components. Custom IDs encode the listing, so buttons keep working
after restarts (discord.py DynamicItem)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord

from tracker.audit import audit
from tracker.bot.embeds import source_name, status_text
from tracker.db.models import Watch
from tracker.domain import StockStatus, format_cents
from tracker.guilds import config_for
from tracker.queries import set_subscribed, set_subscription_regions, toggle_watch
from tracker.watching import (
    ALERT_KEYS,
    WatchRow,
    mute_watches,
    parse_kinds,
    parse_regions,
    remove_watch_ids,
    user_watch_rows,
)
from tracker.watchtest import SampleAlertError, WatchTest, plan_watch_test

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


def everything_text(subscription: Watch | None) -> str:
    """How 'ping me for everything' is set, in a few words."""
    if subscription is None:
        return "off"
    regions = parse_regions(subscription.regions)
    if regions is None:
        return "every region"
    return ", ".join(r.upper() for r in sorted(regions)) + " only"


def watches_embed(
    rows: list[WatchRow], now: datetime, subscription: Watch | None = None
) -> discord.Embed:
    lines = [watch_line(r, now) for r in rows[:_WATCHES_SHOWN]]
    if len(rows) > _WATCHES_SHOWN:
        lines.append(f"…and {len(rows) - _WATCHES_SHOWN} more (all of them are in the panel)")
    if not rows:
        lines.append("Nothing yet. Add one with `/watch` and a SKU, name, or store link.")
    lines += ["", f"**Ping me for every important alert:** {everything_text(subscription)}"]
    embed = discord.Embed(
        title=f"Your watches ({len(rows)})", description="\n".join(lines)[:4000], color=0x5865F2
    )
    if rows:
        embed.set_footer(text="Pick watches, then Remove, Mute 24h, Unmute, or Test.")
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
        title=f"Test: {test.sku} · {test.region.upper()}",
        description="\n".join(lines)[:4000],
        color=0x57F287 if selected.pings else 0xFEE75C,
    )
    embed.set_footer(
        text="Rules and 'ping me for everything' can also ping you; this checks the watch alone."
    )
    return embed


class WatchManager(discord.ui.View):
    """Select some of your watches, then remove, mute, unmute or test them; and switch
    'ping me for every important alert' on or off for a region."""

    def __init__(
        self,
        bot: TrackerBot,
        user_id: str,
        rows: list[WatchRow],
        *,
        guild_id: str | None = None,
        regions: list[str] | None = None,
        subscription: Watch | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = user_id
        self.guild_id = guild_id
        self.regions = list(regions or [])
        self.selected: list[int] = []
        if self.regions:
            self.add_item(self._everything_picker(subscription))
        if rows:
            self._add_watch_controls(rows)

    def _everything_picker(self, subscription: Watch | None) -> discord.ui.Select[Any]:
        current = "off"
        if subscription is not None:
            chosen = parse_regions(subscription.regions)
            current = "all" if chosen is None else ",".join(sorted(chosen))
        options = [("off", "Off"), ("all", "Every region")]
        options += [(r, f"{r.upper()} stores only") for r in self.regions]
        picker: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=f"Ping me for every important alert: {everything_text(subscription)}",
            options=[
                discord.SelectOption(
                    label=f"Every important alert: {label}", value=value, default=value == current
                )
                for value, label in options
            ],
            row=0,
        )

        async def chosen(interaction: discord.Interaction[Any]) -> None:
            await self.set_everything(interaction, picker.values[0])

        picker.callback = chosen  # type: ignore[method-assign]
        return picker

    async def set_everything(self, interaction: discord.Interaction[Any], choice: str) -> None:
        async with self.bot.ctx.sessions() as session, session.begin():
            await set_subscribed(session, self.user_id, choice != "off", self.guild_id)
            if choice != "off":
                await session.flush()
                regions = None if choice == "all" else choice
                await set_subscription_regions(session, self.user_id, regions, self.guild_id)
        await self.refresh(interaction, None)

    async def refresh(self, interaction: discord.Interaction[Any], content: str | None) -> None:
        from tracker.queries import subscription_for

        async with self.bot.ctx.sessions() as session:
            rows = await user_watch_rows(session, self.user_id)
            subscription = await subscription_for(session, self.user_id, self.guild_id)
        await interaction.response.edit_message(
            content=content,
            embed=watches_embed(rows, datetime.now(UTC), subscription),
            view=WatchManager(
                self.bot,
                self.user_id,
                rows,
                guild_id=self.guild_id,
                regions=self.regions,
                subscription=subscription,
            ),
        )

    def _add_watch_controls(self, rows: list[WatchRow]) -> None:
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
        self.picker.row = 1
        self.add_item(self.picker)
        for label, style, action in (
            ("Remove", discord.ButtonStyle.danger, "remove"),
            (f"Mute {MUTE_HOURS}h", discord.ButtonStyle.secondary, "mute"),
            ("Unmute", discord.ButtonStyle.secondary, "unmute"),
            ("Test", discord.ButtonStyle.primary, "test"),
        ):
            button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style, row=2)
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
        if action == "test":
            await self.test(interaction)
            return
        now = datetime.now(UTC)
        async with self.bot.ctx.sessions() as session, session.begin():
            if action == "remove":
                count = await remove_watch_ids(session, self.user_id, self.selected)
            else:
                until = now + timedelta(hours=MUTE_HOURS) if action == "mute" else None
                count = await mute_watches(session, self.user_id, self.selected, until)
        done = {"remove": "Removed", "mute": f"Muted for {MUTE_HOURS}h", "unmute": "Unmuted"}
        await self.refresh(interaction, f"{done[action]} {count} watch(es).")

    async def test(self, interaction: discord.Interaction[Any]) -> None:
        """Send a sample alert for the first selected watch and explain what it covers."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.ctx.sessions() as session:
            test = await plan_watch_test(
                session, self.bot.ctx.settings, self.selected[0], user_id=self.user_id
            )
        if test is None:
            await interaction.followup.send(
                "That watch is gone. Run /watches again.", ephemeral=True
            )
            return
        link: str | None = None
        error: str | None = None
        try:
            link = await self.bot.post_test_alert(test)
        except SampleAlertError as exc:
            error = str(exc)
        async with self.bot.ctx.sessions() as session, session.begin():
            await audit(
                session,
                "watch.test",
                actor_id=self.user_id,
                actor_name=str(interaction.user),
                guild_id=interaction.guild_id,
                target=test.sku,
                details={"alert": test.selected.key, "sent": bool(link), "error": error},
            )
        note = (
            f"Tested the first of {len(self.selected)} selected watches."
            if len(self.selected) > 1
            else None
        )
        await interaction.followup.send(
            content=note, embed=watch_test_embed(test, link, error), ephemeral=True
        )
