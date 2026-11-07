"""Slash commands. Thin: parse input, call tracker.queries / services, render a reply."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from tracker.alerts import AlertPlan
from tracker.audit import audit, describe, recent_audit
from tracker.bot.cleanup import cleanup_messages
from tracker.bot.embeds import KIND_STYLE, alert_content, alert_embed, source_name, status_text
from tracker.bot.views import alert_view
from tracker.db.models import Listing, MarketComp, Product, Valuation, Variant
from tracker.domain import EventKind, Priority, SourceKey, StockStatus, format_cents
from tracker.guilds import (
    ADMIN_FIELDS,
    GuildConfig,
    GuildStatus,
    config_for,
    list_guilds,
    update_config,
)
from tracker.history import EbayTrend, StockHistory, ebay_trend, humanize, stock_history
from tracker.market.ebay import EbayItem
from tracker.market.matching import MatchResult
from tracker.market.scan import ScanReport
from tracker.outbox import RadarEvent, load_events
from tracker.permissions import Role, check_access, resolve_role
from tracker.queries import (
    ListingRow,
    OpportunityRow,
    add_watches,
    forget_user,
    listings_for_url,
    opportunities,
    recent_observations,
    remove_watches,
    resolve_variant,
    search_variants,
    set_priority,
    set_subscribed,
    tracker_stats,
    unifi_listings_for_slug,
    upcoming_restocks,
    user_watches,
    variant_comps,
    variant_listings,
)
from tracker.services.unifi import slug_from_url
from tracker.settings import MarketSettings
from tracker.sources.web import detect_source, parse_money

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot
    from tracker.context import TrackerContext

logger = logging.getLogger(__name__)

_UNIFI_URL_RE = re.compile(r"/(?P<region>[a-z]{2,5})/[a-z]{2}/products/(?P<slug>[^/?#]+)")
_MAX_LINES = 25


def _ts(value: datetime | None, style: str = "R") -> str:
    return f"<t:{int(value.timestamp())}:{style}>" if value else "—"


def _row_line(row: ListingRow) -> str:
    listing = row.listing
    parts = [
        f"**{row.variant.sku}**",
        f"{source_name(listing.source)} {listing.region.upper()}",
        status_text(StockStatus(listing.status)),
        format_cents(listing.price_cents, listing.currency),
    ]
    if row.max_price_cents is not None:
        parts.append(f"ping ≤ {format_cents(row.max_price_cents, listing.currency)}")
    if listing.restock_eta and listing.status != StockStatus.IN_STOCK:
        parts.append(f"ETA {listing.restock_eta.isoformat()}")
    if listing.delisted_at:
        parts.append("delisted")
    return " · ".join(parts)


WITHDRAWN_STATUSES = frozenset({"RegionNotAvailable", "Unavailable"})


def history_lines(listing: Listing, history: StockHistory, now: datetime) -> list[str]:
    """Rarity-relevant facts about one listing's stock history."""
    lines: list[str] = []
    if listing.delisted_at:
        lines.append(f"🗑️ Delisted {_ts(listing.delisted_at)}")
    elif listing.raw_status in WITHDRAWN_STATUSES:
        lines.append("🌍 Withdrawn from this region")
    if history.median_sellout is not None:
        count = len(history.sellout_times)
        lines.append(
            f"⏱️ Sells out in ~{humanize(history.median_sellout)} "
            f"(median of {count} restock{'s' if count != 1 else ''})"
        )
    gone = history.out_of_stock_for(now)
    if gone is not None and not StockStatus(listing.status).is_available:
        plus = "+" if history.unavailable_at_least else ""
        lines.append(f"⌛ Out of stock {humanize(gone)}{plus}")
    return lines


def trend_text(trend: EbayTrend) -> str:
    def pct(value: float | None) -> str:
        return f" ({value:+.0%})" if value is not None else ""

    return (
        f"Sellers {trend.sellers_then} → {trend.sellers_now}{pct(trend.sellers_change)} · "
        f"price floor {format_cents(trend.p25_then)} → {format_cents(trend.p25_now)}"
        f"{pct(trend.price_change)} · {trend.snapshots} daily snapshots"
    )


def _clip(lines: list[str], limit: int = 1024) -> str:
    text = ""
    for line in lines:
        if len(text) + len(line) + 1 > limit:
            break
        text += line + "\n"
    return text.rstrip() or "—"


def scan_embed(report: ScanReport, settings: MarketSettings) -> discord.Embed:
    embed = discord.Embed(
        title="🔎 FlipScout scan",
        description=(
            f"Priced **{report.priced}** items against live eBay asking prices; "
            f"**{len(report.valued)}** had enough sellers to value. "
            f"{report.calls} eBay calls."
        ),
        color=0xF1C40F,
        timestamp=report.at,
    )
    top = []
    for item in report.top(8):
        emoji = status_text(item.status).split(" ")[0]
        eta = f" · ETA {item.restock_eta}" if item.restock_eta and not item.available else ""
        top.append(
            f"{emoji} **{item.sku}** {format_cents(item.retail_cents)} → "
            f"~{format_cents(item.resale_cents)} · **+{format_cents(item.net_cents)}** "
            f"({item.roi:.0%}) · {item.sellers} sellers · {item.confidence}{eta}"
        )
    embed.add_field(name="💰 Top flips", value=_clip(top), inline=False)
    packs = []
    for b in report.best_breakouts(6):
        emoji = status_text(b.status).split(" ")[0]
        packs.append(
            f"{emoji} **{b.pack_sku}** ({b.pack_size}× for {format_cents(b.pack_cents)}) → "
            f"{format_cents(b.unit_cost_cents)}/unit vs ~{format_cents(b.single_resale_cents)}"
            f" · **+{format_cents(b.pack_net_cents)}**/pack · break-even "
            f"{format_cents(b.breakeven_unit_cents)}/unit"
        )
    if packs:
        embed.add_field(name="📦 Pack breakouts", value=_clip(packs), inline=False)
    losers = [
        f"**{i.sku}** {format_cents(i.retail_cents)} → ~{format_cents(i.resale_cents)} · "
        f"{format_cents(i.net_cents)}"
        for i in report.losers(4)
    ]
    if losers:
        embed.add_field(name="📉 Loses money at retail", value=_clip(losers), inline=False)
    if report.errors:
        embed.add_field(name="⚠️ Errors", value=_clip(report.errors[:5]), inline=False)
    embed.set_footer(
        text=(
            f"Asking prices, one per seller (no sold data) · net after {settings.fee_rate:.1%} "
            f"+ {format_cents(settings.fixed_fee_cents)} fees and "
            f"{format_cents(settings.outbound_shipping_cents)} shipping · read-only"
        )
    )
    return embed


def _paginate(lines: list[str], limit: int = _MAX_LINES) -> str:
    text = "\n".join(lines[:limit])
    if len(lines) > limit:
        text += f"\n…and {len(lines) - limit} more"
    return text[:4000]


def _opportunity_line(rank: int, row: OpportunityRow) -> str:
    v = row.valuation
    status = status_text(StockStatus(row.listing.status)).split(" ")[0] if row.listing else "⚪"
    pack = f" ×{row.variant.pack_size}" if row.variant.pack_size > 1 else ""
    roi = f"{v.roi:.0%}" if v.roi is not None else "—"
    return (
        f"`{rank:>2}` {status} **{row.variant.sku}**{pack} · buy {format_cents(v.retail_cents)} "
        f"→ ~{format_cents(v.resale_cents)} · **{format_cents(v.net_cents)}** ({roi}) · "
        f"{v.basis} · {v.confidence}"
    )


def value_embed(
    variant: Variant,
    product: Product | None,
    valuation: Valuation | None,
    comps: list[MarketComp],
    settings: MarketSettings,
    *,
    live: list[tuple[EbayItem, MatchResult]] | None = None,
    trend: EbayTrend | None = None,
) -> discord.Embed:
    title = f"💰 {variant.sku}" + (f" · {product.name}" if product else "")
    embed = discord.Embed(title=title[:256], color=0xF1C40F)
    if valuation is None or valuation.basis is None:
        embed.description = (
            f"Not enough comps yet ({settings.min_comps} needed). Comps come from eBay or `/comp`."
        )
    else:
        v = valuation
        roi = f"{v.roi:.0%}" if v.roi is not None else "—"
        embed.description = (
            f"Buy **{format_cents(v.retail_cents)}** "
            f"({'in stock' if v.available else 'not available now'}) → "
            f"resell **~{format_cents(v.resale_cents)}** → net **{format_cents(v.net_cents)}** "
            f"({roi} ROI)"
        )
        embed.add_field(
            name="Profit breakdown",
            value=(
                f"Resale {format_cents(v.resale_cents)}\n"
                f"− fees {format_cents(v.fees_cents)}\n"
                f"− shipping {format_cents(v.shipping_cents)}\n"
                f"− cost {format_cents(v.cost_cents)}\n"
                f"= **{format_cents(v.net_cents)}**"
            ),
            inline=True,
        )
        sold_range = (
            f"{format_cents(v.sold_p25_cents)}–{format_cents(v.sold_p75_cents)}"
            if v.sold_count
            else "—"
        )
        through = f"{v.sell_through:.0%}" if v.sell_through is not None else "—"
        embed.add_field(
            name="Market",
            value=(
                f"Basis: {v.basis} · {v.confidence} confidence\n"
                f"Sold: {v.sold_count} (30d: {v.sold_30d}) · median "
                f"{format_cents(v.sold_median_cents)} · IQR {sold_range}\n"
                f"Active: {v.active_count} · low {format_cents(v.active_low_cents)} · median "
                f"{format_cents(v.active_median_cents)}\n"
                f"Sell-through (30d): {through}"
            ),
            inline=True,
        )
        embed.set_footer(text=f"Valued {v.computed_at:%Y-%m-%d %H:%M} UTC")
    if trend is not None:
        embed.add_field(
            name=f"📈 eBay trend ({trend.days}d)", value=trend_text(trend), inline=False
        )
    if comps:
        lines = []
        for c in comps:
            ship = f" +{format_cents(c.shipping_cents)}" if c.shipping_cents else ""
            qty = f" ×{c.quantity}" if c.quantity > 1 else ""
            label = (
                f"[{format_cents(c.price_cents)}]({c.url})"
                if c.url
                else format_cents(c.price_cents)
            )
            when = (c.sold_at or c.observed_at).strftime("%b %d")
            note = f" · {c.note[:40]}" if c.note else ""
            lines.append(f"✍️ {label}{ship}{qty} · {c.condition} · {when}{note}")
        embed.add_field(name="Manual comps", value="\n".join(lines)[:1024], inline=False)
    if live:
        lines = []
        for item, match in live[:10]:
            kind = "✅ sold" if item.sold else "🏷️ listed"
            label = (
                f"[{format_cents(item.price_cents)}]({item.url})"
                if item.url
                else (format_cents(item.price_cents))
            )
            verdict = f"×{match.quantity}" if match.matched else f"❌ {match.reason}"
            lines.append(f"{kind} {label} · {verdict}")
        embed.add_field(
            name="Live eBay sample (not stored)", value="\n".join(lines)[:1024], inline=False
        )
    return embed


class TrackerCommands(commands.Cog):
    def __init__(self, bot: TrackerBot) -> None:
        self.bot = bot

    @property
    def ctx(self) -> TrackerContext:
        return self.bot.ctx

    # ── helpers ───────────────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        """Gate every slash command: approved server + the command's minimum role."""
        name = interaction.command.qualified_name if interaction.command else "?"
        guild_config: GuildConfig | None = None
        if interaction.guild_id is not None:
            async with self.ctx.sessions() as session:
                guild_config = await config_for(
                    session, self.ctx.settings.discord, interaction.guild_id
                )
        permissions = getattr(interaction.user, "guild_permissions", None)
        role = resolve_role(
            interaction.user.id,
            owner_ids=self.bot.owner_ids,
            global_admin_ids=self.ctx.settings.discord.admin_user_ids,
            manage_guild=bool(permissions and permissions.manage_guild),
            role_ids=[r.id for r in getattr(interaction.user, "roles", [])],
            guild=guild_config,
        )
        reason = check_access(name, role, guild_config)
        if reason is not None:
            await interaction.response.send_message(f"⛔ {reason}", ephemeral=True)
            return False
        interaction.extras["role"] = role
        interaction.extras["guild_config"] = guild_config
        return True

    @staticmethod
    def _role(interaction: discord.Interaction[Any]) -> Role:
        return interaction.extras.get("role", Role.MEMBER)

    @staticmethod
    def _guild(interaction: discord.Interaction[Any]) -> GuildConfig:
        return interaction.extras["guild_config"]

    async def _audit(
        self,
        interaction: discord.Interaction[Any],
        action: str,
        target: str | None = None,
        **details: Any,
    ) -> None:
        async with self.ctx.sessions() as session, session.begin():
            await audit(
                session,
                action,
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                guild_id=interaction.guild_id,
                target=target,
                details=details or None,
            )

    async def _resolve(self, text: str, region: str | None) -> tuple[list[ListingRow], str | None]:
        """A SKU, product name, or store URL → listings (fetching unknown URLs on demand)."""
        text = text.strip()
        if text.lower().startswith(("http://", "https://")):
            source = detect_source(text)
            if source is None:
                return [], "That URL isn't from a supported store."
            if source is SourceKey.UNIFI:
                match = _UNIFI_URL_RE.search(text)
                if not match:
                    return [], "That doesn't look like a UniFi store product URL."
                url_region, slug = match["region"], match["slug"]
                async with self.ctx.sessions() as session:
                    rows = await unifi_listings_for_slug(session, url_region, slug)
                if not rows:
                    await self.ctx.unifi.poll_product(url_region, slug)
                    async with self.ctx.sessions() as session:
                        rows = await unifi_listings_for_slug(session, url_region, slug)
                return rows, None if rows else "Couldn't load that product page."
            await self.ctx.retail.add_url(text, source)
            async with self.ctx.sessions() as session:
                return await listings_for_url(session, text), None

        async with self.ctx.sessions() as session:
            variant = await resolve_variant(session, text)
            if variant is None:
                return [], f"No product matches `{text}`."
            rows = await variant_listings(session, variant.id, region)
        if not rows:
            return [], f"`{variant.sku}` isn't listed in region `{region}`."
        return rows, None

    # ── autocomplete ──────────────────────────────────────────────────────

    async def product_autocomplete(
        self, interaction: discord.Interaction[Any], current: str
    ) -> list[app_commands.Choice[str]]:
        async with self.ctx.sessions() as session:
            matches = await search_variants(session, current, limit=25)
        return [app_commands.Choice(name=m.label, value=m.sku) for m in matches]

    async def watched_autocomplete(
        self, interaction: discord.Interaction[Any], current: str
    ) -> list[app_commands.Choice[str]]:
        async with self.ctx.sessions() as session:
            rows = await user_watches(session, str(interaction.user.id))
        skus = dict.fromkeys(
            r.variant.sku for r in rows if current.lower() in r.variant.sku.lower()
        )
        return [app_commands.Choice(name=sku, value=sku) for sku in list(skus)[:25]]

    async def region_autocomplete(
        self, interaction: discord.Interaction[Any], current: str
    ) -> list[app_commands.Choice[str]]:
        regions = self.ctx.settings.unifi.regions
        return [app_commands.Choice(name=r.upper(), value=r) for r in regions if current in r]

    # ── watching ──────────────────────────────────────────────────────────

    @app_commands.command(
        description="Get pinged when a product is back in stock, goes EA, or drops in price"
    )
    @app_commands.describe(
        product="SKU, product name, or a store URL (UniFi, Amazon, Best Buy, B&H, Newegg)",
        region="Only this UniFi store region (default: every tracked region)",
        max_price="Only ping when the price is at or below this",
    )
    @app_commands.autocomplete(product=product_autocomplete, region=region_autocomplete)
    async def watch(
        self,
        interaction: discord.Interaction[Any],
        product: str,
        region: str | None = None,
        max_price: app_commands.Range[float, 0.01] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows, error = await self._resolve(product, region)
        if error:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        cap = parse_money(max_price) if max_price is not None else None
        async with self.ctx.sessions() as session, session.begin():
            created = await add_watches(
                session,
                str(interaction.user.id),
                [r.listing.id for r in rows],
                cap,
                str(interaction.guild_id),
            )
        verb = "Watching" if created else "Updated watch for"
        cap_text = f" at or below {format_cents(cap)}" if cap else ""
        listing_lines = _paginate([_row_line(r) for r in rows])
        await interaction.followup.send(
            f"🔔 {verb} {len(rows)} listing(s){cap_text}:\n{listing_lines}", ephemeral=True
        )

    @app_commands.command(description="Stop watching a product")
    @app_commands.describe(product="A product you're watching")
    @app_commands.autocomplete(product=watched_autocomplete)
    async def unwatch(self, interaction: discord.Interaction[Any], product: str) -> None:
        user_id = str(interaction.user.id)
        async with self.ctx.sessions() as session, session.begin():
            rows = [
                r
                for r in await user_watches(session, user_id)
                if r.variant.sku.lower() == product.strip().lower() or r.listing.url == product
            ]
            removed = await remove_watches(session, user_id, [r.listing.id for r in rows])
        message = (
            f"🔕 Removed {removed} watch(es) for `{product}`."
            if removed
            else (f"You aren't watching `{product}`. See `/watches`.")
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(description="List the products you're watching")
    async def watches(self, interaction: discord.Interaction[Any]) -> None:
        async with self.ctx.sessions() as session:
            rows = await user_watches(session, str(interaction.user.id))
        if not rows:
            await interaction.response.send_message(
                "You aren't watching anything yet. Try `/watch`.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"🔔 Your watches ({len(rows)})",
            description=_paginate([_row_line(r) for r in rows]),
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        description="Get pinged for every important alert (back in stock, EA, new)"
    )
    async def subscribe(self, interaction: discord.Interaction[Any]) -> None:
        async with self.ctx.sessions() as session, session.begin():
            changed = await set_subscribed(
                session, str(interaction.user.id), True, str(interaction.guild_id)
            )
        await interaction.response.send_message(
            "🔔 Subscribed to all important alerts." if changed else "You're already subscribed.",
            ephemeral=True,
        )

    @app_commands.command(description="Stop being pinged for every important alert")
    async def unsubscribe(self, interaction: discord.Interaction[Any]) -> None:
        async with self.ctx.sessions() as session, session.begin():
            changed = await set_subscribed(
                session, str(interaction.user.id), False, str(interaction.guild_id)
            )
        await interaction.response.send_message(
            "🔕 Unsubscribed." if changed else "You weren't subscribed.", ephemeral=True
        )

    # ── information ───────────────────────────────────────────────────────

    @app_commands.command(description="Stock, price, and recent history for a product")
    @app_commands.describe(product="SKU, product name, or store URL")
    @app_commands.autocomplete(product=product_autocomplete)
    async def stock(self, interaction: discord.Interaction[Any], product: str) -> None:
        await interaction.response.defer(thinking=True)
        rows, error = await self._resolve(product, None)
        if error:
            await interaction.followup.send(f"⚠️ {error}")
            return
        first = rows[0]
        embed = discord.Embed(title=first.product.name[:256], url=first.listing.url, color=0x5865F2)
        now = datetime.now(UTC)
        async with self.ctx.sessions() as session:
            histories = {
                r.listing.id: await stock_history(session, r.listing.id) for r in rows[:20]
            }
        for row in rows[:20]:
            listing = row.listing
            lines = [
                status_text(StockStatus(listing.status))
                + f" · {format_cents(listing.price_cents, listing.currency)}"
            ]
            lines.extend(history_lines(listing, histories[listing.id], now))
            if listing.restock_eta and listing.status != StockStatus.IN_STOCK:
                lines.append(f"Restock ETA {listing.restock_eta.isoformat()}")
            if listing.is_early_access:
                lines.append("⚡ Early Access")
            lines.append(
                f"In stock {_ts(listing.last_in_stock_at)} · checked {_ts(listing.last_checked_at)}"
            )
            embed.add_field(
                name=f"{row.variant.sku} · {source_name(listing.source)} {listing.region.upper()}"[
                    :256
                ],
                value="\n".join(lines),
                inline=False,
            )
        async with self.ctx.sessions() as session:
            history = await recent_observations(session, first.listing.id, limit=6)
        if history:
            embed.add_field(
                name=f"Recent changes ({first.variant.sku} {first.listing.region.upper()})",
                value="\n".join(
                    f"{_ts(o.observed_at, 'd')} {status_text(StockStatus(o.status))} · "
                    f"{format_cents(o.price_cents, first.listing.currency)}"
                    for o in history
                ),
                inline=False,
            )
        embed.set_footer(
            text=f"{first.product.category or 'uncategorized'} · {first.product.brand}"
        )
        await interaction.followup.send(
            embed=embed, view=alert_view(first.listing.id, first.listing.url)
        )

    @app_commands.command(description="Recent radar events")
    @app_commands.describe(kind="Only this kind of event", limit="How many (max 25)")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name=label, value=k.value)
            for k, (_, label, _) in KIND_STYLE.items()
        ]
    )
    async def radar(
        self,
        interaction: discord.Interaction[Any],
        kind: str | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        async with self.ctx.sessions() as session:
            events = await load_events(
                session, pending_only=False, limit=limit, kinds=[kind] if kind else None
            )
        if not events:
            await interaction.response.send_message("No radar events yet.", ephemeral=True)
            return
        lines = []
        for event in events:
            emoji, label, _ = KIND_STYLE.get(event.kind, ("•", event.kind.value, 0))
            name = f"[{event.sku}]({event.url})" if event.url else (event.sku or "?")
            region = f" {event.region.upper()}" if event.region else ""
            change = (
                f" · {event.old_value or '—'} → {event.new_value or '—'}"
                if event.kind in (EventKind.PRICE_CHANGE, EventKind.RESTOCK_ETA)
                else ""
            )
            lines.append(f"{_ts(event.occurred_at)} {emoji} {label} · {name}{region}{change}")
        embed = discord.Embed(title="📡 Radar", description=_paginate(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Sold-out items with an announced restock date")
    @app_commands.describe(days="Look this many days ahead")
    async def restocks(
        self, interaction: discord.Interaction[Any], days: app_commands.Range[int, 1, 120] = 14
    ) -> None:
        async with self.ctx.sessions() as session:
            rows = await upcoming_restocks(session, datetime.now(UTC).date(), days)
        if not rows:
            await interaction.response.send_message(
                f"No announced restocks in the next {days} days.", ephemeral=True
            )
            return
        lines = [
            f"`{r.listing.restock_eta}` **{r.variant.sku}** {r.listing.region.upper()} · "
            f"{format_cents(r.listing.price_cents, r.listing.currency)}"
            for r in rows
        ]
        embed = discord.Embed(
            title=f"📅 Restocks in the next {days} days ({len(rows)})",
            description=_paginate(lines, 40),
            color=0x3BA55C,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Tracker health and catalog summary")
    async def status(self, interaction: discord.Interaction[Any]) -> None:
        now = datetime.now(UTC)
        async with self.ctx.sessions() as session:
            stats = await tracker_stats(session, now)
        embed = discord.Embed(title="📡 Tracker status", color=0x57F287)
        uptime = now - self.ctx.started_at
        embed.add_field(name="Uptime", value=str(uptime).split(".")[0], inline=True)
        embed.add_field(
            name="Watches", value=f"{stats.watches} · {stats.subscribers} subscribers", inline=True
        )
        embed.add_field(
            name="Events",
            value=f"{stats.events_24h} in 24h · {stats.pending_events} pending",
            inline=True,
        )
        regions = sorted({region for region, _ in stats.status_counts})
        for region in regions:
            counts = {s: n for (r, s), n in stats.status_counts.items() if r == region}
            lines = [f"{status_text(StockStatus(s))}: {n}" for s, n in sorted(counts.items())]
            lines.append(f"Last check {_ts(stats.last_checked.get(region))}")
            embed.add_field(
                name=f"{region.upper()} ({sum(counts.values())})", value="\n".join(lines)
            )
        mono = time.monotonic()  # the scheduler's clock
        jobs = []
        for job in self.ctx.scheduler.jobs.values():
            state = "running" if job.running else f"next in {max(0, int(job.next_run - mono))}s"
            health = f" · ⚠️ {job.failures} failures" if job.failures else ""
            jobs.append(f"`{job.name}` {job.runs} runs · {state}{health}")
        embed.add_field(name="Jobs", value="\n".join(jobs)[:1024] or "—", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── market ────────────────────────────────────────────────────────────

    @app_commands.command(description="Best resale opportunities, ranked by estimated profit")
    @app_commands.describe(
        min_profit="Minimum estimated net profit in dollars",
        available="Only items you can buy right now",
        category="Category filter, e.g. wifi, switching, cloud-gateways",
        limit="How many (max 25)",
    )
    async def opportunities(
        self,
        interaction: discord.Interaction[Any],
        min_profit: app_commands.Range[float, 0] = 0.0,
        available: bool = False,
        category: str | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        async with self.ctx.sessions() as session:
            rows = await opportunities(
                session,
                limit=limit,
                min_profit_cents=round(min_profit * 100),
                available_only=available,
                category=category,
            )
        if not rows:
            hint = (
                "Comps are being collected from eBay; check back soon."
                if self.ctx.market.collecting
                else "No eBay credentials set: add comps with `/comp`, or set "
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET."
            )
            await interaction.response.send_message(
                f"No opportunities match yet. {hint}", ephemeral=True
            )
            return
        lines = [_opportunity_line(rank, row) for rank, row in enumerate(rows, start=1)]
        embed = discord.Embed(
            title="💰 Resale opportunities", description=_paginate(lines), color=0xF1C40F
        )
        settings = self.ctx.settings.market
        embed.set_footer(
            text=f"Net after {settings.fee_rate:.1%} + {format_cents(settings.fixed_fee_cents)} "
            f"fees, {format_cents(settings.outbound_shipping_cents)} shipping, "
            f"{settings.purchase_tax_rate:.1%} tax · /value for details"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Resale valuation, profit breakdown, and comps for a product")
    @app_commands.describe(product="SKU or product name", refresh="[admin] Fetch fresh eBay comps")
    @app_commands.autocomplete(product=product_autocomplete)
    async def value(
        self, interaction: discord.Interaction[Any], product: str, refresh: bool = False
    ) -> None:
        if refresh and self._role(interaction) < Role.OWNER:
            await interaction.response.send_message(
                "⛔ Only the owner can refresh eBay data (it uses the API quota).", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.ctx.sessions() as session:
            variant = await resolve_variant(session, product)
        if variant is None:
            await interaction.followup.send(f"⚠️ No product matches `{product}`.", ephemeral=True)
            return
        live = None
        if refresh and self.ctx.market.collecting:
            live = (await self.ctx.market.collect(variant.id)).items
        async with self.ctx.sessions() as session:
            valuation = await session.get(Valuation, variant.id)
            comps = await variant_comps(session, variant.id, limit=8)
            product_row = await session.get(Product, variant.product_id)
            trend = await ebay_trend(session, variant.id)
        embed = value_embed(
            variant, product_row, valuation, comps, self.ctx.settings.market, live=live, trend=trend
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(description="[admin] Record a resale comp (your sale or one you saw)")
    @app_commands.describe(
        product="SKU or product name",
        price="Sale price per listing, in dollars",
        shipping="Shipping the buyer paid",
        quantity="Units in that sale (lots)",
        url="Link to the listing",
        note="Where it sold / anything useful",
    )
    @app_commands.autocomplete(product=product_autocomplete)
    @app_commands.choices(
        condition=[
            app_commands.Choice(name=label, value=value)
            for value, label in (("new", "New"), ("open_box", "Open box"), ("used", "Used"))
        ]
    )
    async def comp(
        self,
        interaction: discord.Interaction[Any],
        product: str,
        price: app_commands.Range[float, 0.01],
        shipping: app_commands.Range[float, 0] | None = None,
        condition: str = "new",
        quantity: app_commands.Range[int, 1, 50] = 1,
        url: str | None = None,
        note: str | None = None,
    ) -> None:
        async with self.ctx.sessions() as session:
            variant = await resolve_variant(session, product)
        if variant is None:
            await interaction.response.send_message(
                f"⚠️ No product matches `{product}`.", ephemeral=True
            )
            return
        await self._audit(interaction, "market.comp", variant.sku, price=price, quantity=quantity)
        result = await self.ctx.market.add_manual_comp(
            variant.id,
            round(price * 100),
            shipping_cents=round(shipping * 100) if shipping is not None else None,
            condition=condition,
            quantity=quantity,
            url=url,
            note=note,
            created_by=str(interaction.user.id),
        )
        summary = (
            f"net {format_cents(result.net_cents)} ({result.roi:.0%} ROI), "
            f"{result.basis} basis, {result.confidence} confidence"
            if result and result.net_cents is not None and result.roi is not None
            else f"need {self.ctx.settings.market.min_comps} comps for a valuation"
        )
        await interaction.response.send_message(
            f"✅ Recorded {format_cents(round(price * 100))} comp for `{variant.sku}`: {summary}.",
            ephemeral=True,
        )

    @app_commands.command(description="[owner] Live profit scan of the store against eBay")
    @app_commands.describe(
        limit="How many items to price (one eBay call each)",
        min_retail="Only items costing at least this much at retail ($)",
        public="Post the result in this channel instead of only to you",
    )
    async def scan(
        self,
        interaction: discord.Interaction[Any],
        limit: app_commands.Range[int, 5, 60] = 25,
        min_retail: app_commands.Range[float, 0, 10000] = 100.0,
        public: bool = False,
    ) -> None:
        if not self.ctx.market.collecting:
            await interaction.response.send_message(
                "⚠️ eBay isn't configured (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).", ephemeral=True
            )
            return
        if self.ctx.market.scanning:
            await interaction.response.send_message(
                "⏳ A scan is already running; try again in a minute.", ephemeral=True
            )
            return
        show_public = public and self._guild(interaction).show_valuations
        await interaction.response.defer(ephemeral=not show_public, thinking=True)
        report = await self.ctx.market.scan(limit=limit, min_retail_cents=round(min_retail * 100))
        await self._audit(interaction, "market.scan", f"{report.priced} items", calls=report.calls)
        await interaction.followup.send(
            embed=scan_embed(report, self.ctx.settings.market), ephemeral=not show_public
        )

    # ── admin ─────────────────────────────────────────────────────────────

    @app_commands.command(description="[admin] Check a product right now")
    @app_commands.autocomplete(product=product_autocomplete)
    async def check(self, interaction: discord.Interaction[Any], product: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._audit(interaction, "stock.check", product)
        rows, error = await self._resolve(product, None)
        if error:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        pages = {
            (r.listing.region, slug)
            for r in rows
            if r.listing.source == SourceKey.UNIFI and (slug := slug_from_url(r.listing.url))
        }
        for region, slug in pages:
            await self.ctx.unifi.poll_product(region, slug)
        for row in rows:
            if row.listing.source != SourceKey.UNIFI:
                await self.ctx.retail.poll(
                    row.listing.id, row.listing.url, SourceKey(row.listing.source)
                )
        await self.ctx.dispatcher.dispatch()
        async with self.ctx.sessions() as session:
            refreshed = [
                r
                for r in await variant_listings(session, rows[0].variant.id)
                if r.listing.id in {x.listing.id for x in rows}
            ] or rows
        await interaction.followup.send(
            "🔄 Checked now:\n" + _paginate([_row_line(r) for r in refreshed]), ephemeral=True
        )

    @app_commands.command(description="[admin] Set how often a product is polled")
    @app_commands.autocomplete(product=product_autocomplete)
    @app_commands.choices(
        level=[app_commands.Choice(name=p.value, value=p.value) for p in Priority]
    )
    async def priority(
        self, interaction: discord.Interaction[Any], product: str, level: str
    ) -> None:
        async with self.ctx.sessions() as session, session.begin():
            variant = await resolve_variant(session, product)
            changed = await set_priority(session, variant.id, level) if variant else 0
        if variant:
            await self._audit(interaction, "stock.priority", variant.sku, level=level)
        await interaction.response.send_message(
            f"✅ `{variant.sku}` priority → **{level}** on {changed} listing(s)."
            if variant
            else f"⚠️ No product matches `{product}`.",
            ephemeral=True,
        )

    @app_commands.command(
        description="[admin] Post a sample alert here to test formatting and push"
    )
    @app_commands.describe(kind="Alert type", delay="Seconds to wait (leave the app to test push)")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name=label, value=k.value)
            for k, (_, label, _) in KIND_STYLE.items()
        ]
    )
    async def testalert(
        self,
        interaction: discord.Interaction[Any],
        kind: str = EventKind.BACK_IN_STOCK.value,
        delay: app_commands.Range[int, 0, 60] = 0,
    ) -> None:
        if interaction.channel is None:
            return
        await self._audit(interaction, "alert.test", kind)
        await interaction.response.send_message(
            f"🧪 Sending a sample `{kind}` alert in {delay}s.", ephemeral=True
        )
        event = RadarEvent(
            id=0,
            kind=EventKind(kind),
            occurred_at=datetime.now(UTC),
            old_value="$199.00" if kind == EventKind.PRICE_CHANGE else None,
            new_value="$189.00" if kind == EventKind.PRICE_CHANGE else None,
            listing_id=None,
            source="unifi",
            region="us",
            url="https://store.ui.com/us/en/products/u7-pro",
            sku="U7-Pro",
            product_name="Access Point U7 Pro (TEST)",
            category="wifi-flagship",
            pack_size=1,
            status=StockStatus.IN_STOCK,
            price_cents=18900,
            currency="USD",
            restock_eta=None,
            is_early_access=kind == EventKind.EARLY_ACCESS,
        )
        plan = AlertPlan(
            event=event,
            important=True,
            channel_id=None,
            mention_user_ids=[str(interaction.user.id)],
        )
        await asyncio.sleep(delay)
        await interaction.channel.send(  # type: ignore[union-attr]
            content=alert_content(plan),
            embed=alert_embed(event),
            view=alert_view(None, event.url),
        )

    @app_commands.command(description="[admin] Delete this bot's old messages now")
    async def cleanup(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self._guild(interaction)
        channels = [c for c in (guild.alert_channel_id, guild.radar_channel_id) if c]
        deleted = await cleanup_messages(
            self.bot, channels, self.ctx.settings.cleanup, datetime.now(UTC)
        )
        await self._audit(interaction, "messages.cleanup", deleted=deleted)
        await interaction.followup.send(f"🧹 Deleted {deleted} old message(s).", ephemeral=True)

    # ── members: privacy ──────────────────────────────────────────────────

    @app_commands.command(description="Delete everything the tracker stores about you")
    @app_commands.describe(confirm="Set to True to permanently delete your watches and settings")
    async def forgetme(self, interaction: discord.Interaction[Any], confirm: bool = False) -> None:
        if not confirm:
            await interaction.response.send_message(
                "This deletes all your watches and subscriptions in every server. "
                "Run `/forgetme confirm:True` to proceed.",
                ephemeral=True,
            )
            return
        async with self.ctx.sessions() as session, session.begin():
            counts = await forget_user(session, str(interaction.user.id))
            # Logged without the member's identity: the point is to forget them.
            await audit(session, "member.forget", guild_id=interaction.guild_id, details=counts)
        await interaction.response.send_message(
            f"🗑️ Done: removed {counts['watches']} watch(es) and subscription(s).", ephemeral=True
        )

    @app_commands.command(description="What this bot does and what it stores about you")
    async def about(self, interaction: discord.Interaction[Any]) -> None:
        embed = discord.Embed(
            title="About this tracker",
            color=0x5865F2,
            description=(
                "A private stock and price tracker run by this server's owner.\n\n"
                "**What it stores about you:** your Discord user ID, the products you watch "
                "(with any max price), and subscriptions. Nothing else; it can't read messages.\n"
                "**Delete it:** `/forgetme confirm:True` removes it all, in every server.\n"
                "**Who sees it:** only the bot's operator."
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── servers ───────────────────────────────────────────────────────────

    server = app_commands.Group(name="server", description="This server's tracker settings")

    @server.command(name="settings", description="Show this server's alert settings")
    async def server_settings(self, interaction: discord.Interaction[Any]) -> None:
        g = self._guild(interaction)

        def channel(cid: int | None) -> str:
            return f"<#{cid}>" if cid else "—"

        embed = discord.Embed(title=f"⚙️ {interaction.guild.name if interaction.guild else ''}")
        embed.add_field(name="Alerts channel", value=channel(g.alert_channel_id))
        embed.add_field(
            name="Radar channel",
            value=channel(g.radar_channel_id) + ("" if g.radar_channel_id else " (uses alerts)"),
        )
        embed.add_field(name="Mention", value=g.alert_mention or "—")
        embed.add_field(
            name="Admin role", value=f"<@&{g.admin_role_id}>" if g.admin_role_id else "—"
        )
        embed.add_field(
            name="Pings for", value=", ".join(sorted(g.mention_kinds)) or "—", inline=False
        )
        embed.add_field(name="Cooldown", value=f"{g.cooldown_minutes:g} min")
        embed.add_field(name="Digest after", value=f"{g.digest_threshold} events")
        embed.add_field(name="Resale valuations", value="shown" if g.show_valuations else "hidden")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @server.command(name="configure", description="Change this server's alert settings")
    @app_commands.describe(
        alerts_channel="Where important alerts (with pings) go",
        radar_channel="Where routine radar events go",
        mention_role="Role to ping on important alerts",
        admin_role="Role whose members can manage these settings",
        ping_kinds="Comma-separated: back_in_stock, early_access, new_listing, relisted, deal",
        cooldown_minutes="Suppress repeats of the same alert for this long",
        digest_threshold="Combine more routine events than this into a digest",
        clear_radar="Send radar events to the alerts channel",
        clear_mention="Stop pinging a role",
    )
    async def server_configure(
        self,
        interaction: discord.Interaction[Any],
        alerts_channel: discord.TextChannel | None = None,
        radar_channel: discord.TextChannel | None = None,
        mention_role: discord.Role | None = None,
        admin_role: discord.Role | None = None,
        ping_kinds: str | None = None,
        cooldown_minutes: app_commands.Range[float, 0, 1440] | None = None,
        digest_threshold: app_commands.Range[int, 1, 100] | None = None,
        clear_radar: bool = False,
        clear_mention: bool = False,
    ) -> None:
        changes: dict[str, Any] = {}
        warnings: list[str] = []
        for key, channel in (
            ("alert_channel_id", alerts_channel),
            ("radar_channel_id", radar_channel),
        ):
            if channel is None:
                continue
            me = channel.guild.me
            perms = channel.permissions_for(me) if me else None
            if perms is None or not (perms.send_messages and perms.embed_links):
                warnings.append(f"I can't post embeds in {channel.mention}; fix its permissions.")
            changes[key] = str(channel.id)
        if clear_radar:
            changes["radar_channel_id"] = None
        if mention_role is not None:
            changes["alert_mention"] = mention_role.mention
        if clear_mention:
            changes["alert_mention"] = None
        if admin_role is not None:
            changes["admin_role_id"] = str(admin_role.id)
        if ping_kinds is not None:
            kinds = [k.strip() for k in ping_kinds.split(",") if k.strip()]
            unknown = [k for k in kinds if k not in EventKind]
            if unknown:
                await interaction.response.send_message(
                    f"⚠️ Unknown kinds: {', '.join(unknown)}", ephemeral=True
                )
                return
            changes["mention_kinds"] = ",".join(kinds)
        if cooldown_minutes is not None:
            changes["cooldown_minutes"] = float(cooldown_minutes)
        if digest_threshold is not None:
            changes["digest_threshold"] = int(digest_threshold)
        if not changes:
            await interaction.response.send_message("Nothing to change.", ephemeral=True)
            return
        async with self.ctx.sessions() as session, session.begin():
            await update_config(
                session,
                str(interaction.guild_id),
                changes,
                allowed=ADMIN_FIELDS,
                actor_id=str(interaction.user.id),
                actor_name=str(interaction.user),
            )
        note = ("\n" + "\n".join(f"⚠️ {w}" for w in warnings)) if warnings else ""
        await interaction.response.send_message(
            f"✅ Updated: {', '.join(sorted(changes))}.{note}", ephemeral=True
        )

    @server.command(
        name="valuations", description="[owner] Show or hide eBay-derived resale numbers here"
    )
    async def server_valuations(self, interaction: discord.Interaction[Any], enabled: bool) -> None:
        async with self.ctx.sessions() as session, session.begin():
            await update_config(
                session,
                str(interaction.guild_id),
                {"show_valuations": enabled},
                allowed=frozenset({"show_valuations"}),
                actor_id=str(interaction.user.id),
                actor_name=str(interaction.user),
            )
        note = (
            " eBay's license treats showing these to other people as displaying eBay content; "
            "keep it to servers that are just you."
            if enabled
            else ""
        )
        await interaction.response.send_message(
            f"✅ Resale valuations {'shown' if enabled else 'hidden'} in this server.{note}",
            ephemeral=True,
        )

    @server.command(name="audit", description="Recent admin actions (this server; owner sees all)")
    async def server_audit(
        self, interaction: discord.Interaction[Any], limit: app_commands.Range[int, 1, 25] = 15
    ) -> None:
        scope = None if self._role(interaction) >= Role.OWNER else str(interaction.guild_id)
        async with self.ctx.sessions() as session:
            entries = await recent_audit(session, limit=limit, guild_id=scope)
        text = "\n".join(f"`{describe(e)}`"[:300] for e in entries) or "No entries."
        embed = discord.Embed(title="🧾 Audit log", description=text[:4000], color=0x99AAB5)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @server.command(name="list", description="[owner] All servers and their approval status")
    async def server_list(self, interaction: discord.Interaction[Any]) -> None:
        async with self.ctx.sessions() as session:
            rows = await list_guilds(session)
        icon = {GuildStatus.APPROVED: "✅", GuildStatus.PENDING: "⏳", GuildStatus.BLOCKED: "⛔"}
        lines = [
            f"{icon.get(GuildStatus(r.status), '?')} "
            f"**{discord.utils.escape_markdown(r.name or '?')}** "
            f"`{r.guild_id}`{' · valuations' if r.show_valuations else ''}"
            for r in rows
        ]
        embed = discord.Embed(title="Servers", description=_paginate(lines, 40), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @server.command(name="approve", description="[owner] Approve a server by ID")
    async def server_approve(self, interaction: discord.Interaction[Any], guild_id: str) -> None:
        await self._decide(interaction, guild_id, GuildStatus.APPROVED)

    @server.command(name="block", description="[owner] Block a server by ID (the bot leaves it)")
    async def server_block(self, interaction: discord.Interaction[Any], guild_id: str) -> None:
        await self._decide(interaction, guild_id, GuildStatus.BLOCKED)

    async def _decide(
        self, interaction: discord.Interaction[Any], guild_id: str, status: GuildStatus
    ) -> None:
        if not guild_id.isdigit():
            await interaction.response.send_message("⚠️ Server IDs are numbers.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.decide_guild(int(guild_id), status, interaction.user)
        await interaction.followup.send(f"✅ Server `{guild_id}` → {status.value}.", ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction[Any], error: app_commands.AppCommandError
    ) -> None:
        logger.exception(
            "Slash command %s failed", getattr(interaction.command, "name", "?"), exc_info=error
        )
        message = "⚠️ Something went wrong. The error was logged."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
