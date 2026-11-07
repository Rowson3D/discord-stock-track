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
from tracker.bot.cleanup import cleanup_messages
from tracker.bot.embeds import KIND_STYLE, alert_content, alert_embed, source_name, status_text
from tracker.bot.views import alert_view
from tracker.db.models import MarketComp, Product, Valuation, Variant
from tracker.domain import EventKind, Priority, SourceKey, StockStatus, format_cents
from tracker.outbox import RadarEvent, load_events
from tracker.queries import (
    ListingRow,
    OpportunityRow,
    add_watches,
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
    if comps:
        lines = []
        for c in comps:
            kind = "✅ sold" if c.sold else "🏷️ listed"
            ship = f" +{format_cents(c.shipping_cents)}" if c.shipping_cents else ""
            qty = f" ×{c.quantity}" if c.quantity > 1 else ""
            label = (
                f"[{format_cents(c.price_cents)}]({c.url})"
                if c.url
                else format_cents(c.price_cents)
            )
            when = (c.sold_at or c.observed_at).strftime("%b %d")
            lines.append(f"{kind} {label}{ship}{qty} · {c.condition} · {c.source} · {when}")
        embed.add_field(name="Recent comps", value="\n".join(lines)[:1024], inline=False)
    return embed


class TrackerCommands(commands.Cog):
    def __init__(self, bot: TrackerBot) -> None:
        self.bot = bot

    @property
    def ctx(self) -> TrackerContext:
        return self.bot.ctx

    # ── helpers ───────────────────────────────────────────────────────────

    def _is_admin(self, interaction: discord.Interaction[Any]) -> bool:
        if str(interaction.user.id) in self.ctx.settings.discord.admin_user_ids:
            return True
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and permissions.manage_guild)

    async def _require_admin(self, interaction: discord.Interaction[Any]) -> bool:
        if self._is_admin(interaction):
            return True
        await interaction.response.send_message(
            "⛔ Admins only (Manage Server permission or DISCORD_ADMIN_USER_IDS).", ephemeral=True
        )
        return False

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
                session, str(interaction.user.id), [r.listing.id for r in rows], cap
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
            changed = await set_subscribed(session, str(interaction.user.id), True)
        await interaction.response.send_message(
            "🔔 Subscribed to all important alerts." if changed else "You're already subscribed.",
            ephemeral=True,
        )

    @app_commands.command(description="Stop being pinged for every important alert")
    async def unsubscribe(self, interaction: discord.Interaction[Any]) -> None:
        async with self.ctx.sessions() as session, session.begin():
            changed = await set_subscribed(session, str(interaction.user.id), False)
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
        for row in rows[:20]:
            listing = row.listing
            lines = [
                status_text(StockStatus(listing.status))
                + f" · {format_cents(listing.price_cents, listing.currency)}"
            ]
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
        if refresh and not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.ctx.sessions() as session:
            variant = await resolve_variant(session, product)
        if variant is None:
            await interaction.followup.send(f"⚠️ No product matches `{product}`.", ephemeral=True)
            return
        if refresh and self.ctx.market.collecting:
            await self.ctx.market.collect(variant.id)
        async with self.ctx.sessions() as session:
            valuation = await session.get(Valuation, variant.id)
            comps = await variant_comps(session, variant.id, limit=8)
            product_row = await session.get(Product, variant.product_id)
        embed = value_embed(variant, product_row, valuation, comps, self.ctx.settings.market)
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
        if not await self._require_admin(interaction):
            return
        async with self.ctx.sessions() as session:
            variant = await resolve_variant(session, product)
        if variant is None:
            await interaction.response.send_message(
                f"⚠️ No product matches `{product}`.", ephemeral=True
            )
            return
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

    # ── admin ─────────────────────────────────────────────────────────────

    @app_commands.command(description="[admin] Check a product right now")
    @app_commands.autocomplete(product=product_autocomplete)
    async def check(self, interaction: discord.Interaction[Any], product: str) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
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
        if not await self._require_admin(interaction):
            return
        async with self.ctx.sessions() as session, session.begin():
            variant = await resolve_variant(session, product)
            changed = await set_priority(session, variant.id, level) if variant else 0
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
        if not await self._require_admin(interaction):
            return
        if interaction.channel is None:
            return
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
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = self.ctx.settings
        channels = [
            c for c in (settings.discord.channel_id, settings.discord.radar_channel_id) if c
        ]
        deleted = await cleanup_messages(self.bot, channels, settings.cleanup, datetime.now(UTC))
        await interaction.followup.send(f"🧹 Deleted {deleted} old message(s).", ephemeral=True)

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
