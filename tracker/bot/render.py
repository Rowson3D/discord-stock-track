"""Embed text shared by the bot's screens: stock history, profit, flips, scans."""

from __future__ import annotations

from datetime import datetime

import discord

from tracker.bot.embeds import item_name, source_name, status_text
from tracker.db.models import Listing, MarketComp, Product, Valuation, Variant
from tracker.domain import StockStatus, format_cents
from tracker.history import EbayTrend, StockHistory, humanize
from tracker.market.ebay import EbayItem
from tracker.market.matching import MatchResult
from tracker.market.scan import ScanReport
from tracker.queries import ListingRow, OpportunityRow
from tracker.settings import MarketSettings

MAX_LINES = 25


def ts(value: datetime | None, style: str = "R") -> str:
    return f"<t:{int(value.timestamp())}:{style}>" if value else "—"


def row_line(row: ListingRow) -> str:
    listing = row.listing
    parts = [
        f"**{item_name(row.variant.sku, row.product.name)}**",
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
        lines.append(f"🗑️ Delisted {ts(listing.delisted_at)}")
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


def clip(lines: list[str], limit: int = 1024) -> str:
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
    embed.add_field(name="💰 Top flips", value=clip(top), inline=False)
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
        embed.add_field(name="📦 Pack breakouts", value=clip(packs), inline=False)
    losers = [
        f"**{i.sku}** {format_cents(i.retail_cents)} → ~{format_cents(i.resale_cents)} · "
        f"{format_cents(i.net_cents)}"
        for i in report.losers(4)
    ]
    if losers:
        embed.add_field(name="📉 Loses money at retail", value=clip(losers), inline=False)
    if report.errors:
        embed.add_field(name="⚠️ Errors", value=clip(report.errors[:5]), inline=False)
    embed.set_footer(
        text=(
            f"Asking prices, one per seller (no sold data) · net after {settings.fee_rate:.1%} "
            f"+ {format_cents(settings.fixed_fee_cents)} fees and "
            f"{format_cents(settings.outbound_shipping_cents)} shipping · read-only"
        )
    )
    return embed


def paginate(lines: list[str], limit: int = MAX_LINES) -> str:
    text = "\n".join(lines[:limit])
    if len(lines) > limit:
        text += f"\n…and {len(lines) - limit} more"
    return text[:4000]


def opportunity_line(rank: int, row: OpportunityRow) -> str:
    v = row.valuation
    status = status_text(StockStatus(row.listing.status)).split(" ")[0] if row.listing else "⚪"
    pack = f" ×{row.variant.pack_size}" if row.variant.pack_size > 1 else ""
    roi = f"{v.flip_roi:.0%}" if v.flip_roi is not None else "—"
    resale = v.scarcity_resale_cents if v.flip_basis == "scarcity" else v.resale_cents
    evidence = f"sold out {v.scarcity_days}d" if v.flip_basis == "scarcity" else v.flip_basis
    name = item_name(row.variant.sku, row.product.name)
    return (
        f"`{rank:>2}` {status} **{name}**{pack} · buy {format_cents(v.retail_cents)} "
        f"→ ~{format_cents(resale)} · **{format_cents(v.flip_net_cents)}** ({roi}) · "
        f"{evidence} · {v.flip_confidence} confidence"
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
    name = product.name if product else None
    label = item_name(variant.sku, name, 256)
    title = f"💰 {label}" + (f" · {name}" if name and label != name else "")
    embed = discord.Embed(title=title[:256], color=0xF1C40F)
    if valuation is None or valuation.basis is None:
        embed.description = (
            f"Not enough comps yet ({settings.min_comps} needed). "
            "Comps come from eBay or Record a sale."
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
        notes = []
        if v.basis == "capped":
            notes.append(
                "🧢 **Capped:** the store has it in stock, so eBay asking prices are capped at "
                f"what a buyer would pay the store (~{format_cents(v.resale_cents)}). "
                "Asking prices above that are resellers hoping, not real resale value."
            )
        if v.scarcity_days and v.scarcity_resale_cents:
            scarce_net = (
                f" → net **{format_cents(v.scarcity_net_cents)}** ({v.scarcity_roi:.0%})"
                if v.scarcity_net_cents is not None and v.scarcity_roi is not None
                else ""
            )
            notes.append(
                f"🔥 **Sold-out market:** ~{format_cents(v.scarcity_resale_cents)} across "
                f"{v.scarcity_days} day(s) the store was sold out{scarce_net}."
            )
        if v.flip_basis:
            notes.append(
                f"✅ **Credible flip** ({v.flip_basis}, {v.flip_confidence}): "
                f"{format_cents(v.flip_net_cents)} per unit."
            )
        else:
            notes.append(
                "⚠️ **No credible flip yet:** needs eBay sold data, your own sales (Record a sale), "
                "or prices recorded while the store is sold out."
            )
        embed.add_field(name="Verdict", value="\n".join(notes)[:1024], inline=False)
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
