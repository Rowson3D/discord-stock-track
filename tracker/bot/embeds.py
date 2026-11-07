"""Discord embed builders (pure functions of domain data)."""

from __future__ import annotations

from collections.abc import Sequence

import discord

from tracker.alerts import AlertPlan, is_price_drop
from tracker.domain import EventKind, SourceKey, StockStatus, format_cents
from tracker.outbox import RadarEvent
from tracker.sources.web import SOURCE_NAMES

KIND_STYLE: dict[EventKind, tuple[str, str, int]] = {
    EventKind.BACK_IN_STOCK: ("🟢", "Back in stock", 0x57F287),
    EventKind.EARLY_ACCESS: ("⚡", "Early Access", 0xEB459E),
    EventKind.NEW_LISTING: ("🆕", "New listing", 0x5865F2),
    EventKind.RELISTED: ("♻️", "Relisted", 0x5865F2),
    EventKind.SOLD_OUT: ("🔴", "Sold out", 0xED4245),
    EventKind.PRICE_CHANGE: ("💲", "Price change", 0xFEE75C),
    EventKind.RESTOCK_ETA: ("📅", "Restock ETA", 0x3BA55C),
    EventKind.STATUS_CHANGE: ("🔁", "Status change", 0x99AAB5),
    EventKind.DELISTED: ("🗑️", "Delisted", 0x4F545C),
}
STATUS_STYLE: dict[StockStatus, tuple[str, str]] = {
    StockStatus.IN_STOCK: ("🟢", "In stock"),
    StockStatus.LOW_STOCK: ("🟡", "Low stock"),
    StockStatus.OUT_OF_STOCK: ("🔴", "Sold out"),
    StockStatus.COMING_SOON: ("🕒", "Coming soon"),
    StockStatus.UNKNOWN: ("⚪", "Unknown"),
}


def style(event: RadarEvent) -> tuple[str, str, int]:
    emoji, label, color = KIND_STYLE.get(event.kind, ("•", event.kind.value, 0x99AAB5))
    if event.kind is EventKind.PRICE_CHANGE:
        emoji, label = ("📉", "Price drop") if is_price_drop(event) else ("📈", "Price increase")
    return emoji, label, color


def status_text(status: StockStatus) -> str:
    emoji, label = STATUS_STYLE[status]
    return f"{emoji} {label}"


def source_name(source: str | None) -> str:
    try:
        return SOURCE_NAMES[SourceKey(source or "")]
    except ValueError:
        return source or "?"


def alert_content(plan: AlertPlan) -> str:
    """Short text line sent with the embed: phones only push for message text, not embeds."""
    event = plan.event
    emoji, label, _ = style(event)
    price = format_cents(event.price_cents, event.currency) if event.price_cents else None
    parts = [f"{emoji} **{label}**", event.sku or event.product_name or "item"]
    if price:
        parts.append(price)
    if event.region and event.region != "us":
        parts.append(event.region.upper())
    line = " · ".join(parts)
    return f"{line} {plan.mentions}".strip()[:2000]


def alert_embed(event: RadarEvent) -> discord.Embed:
    emoji, label, color = style(event)
    title = f"{emoji} {label}: {event.product_name or event.sku or 'Unknown item'}"
    embed = discord.Embed(
        title=title[:256], url=event.url, color=color, timestamp=event.occurred_at
    )

    sku = event.sku or "—"
    if event.pack_size > 1:
        sku += f" ({event.pack_size}-pack)"
    embed.add_field(name="SKU", value=sku, inline=True)
    embed.add_field(
        name="Price", value=format_cents(event.price_cents, event.currency), inline=True
    )
    embed.add_field(name="Status", value=status_text(event.status), inline=True)

    if event.kind in (EventKind.PRICE_CHANGE, EventKind.RESTOCK_ETA, EventKind.STATUS_CHANGE):
        embed.add_field(
            name="Change",
            value=f"{event.old_value or '—'} → {event.new_value or '—'}",
            inline=False,
        )
    if event.restock_eta and event.kind is not EventKind.RESTOCK_ETA:
        embed.add_field(name="Restock ETA", value=event.restock_eta.isoformat(), inline=True)
    if event.is_early_access:
        embed.add_field(name="Access", value="⚡ Early Access", inline=True)
    store = source_name(event.source)
    if event.region:
        store += f" · {event.region.upper()}"
    embed.set_footer(text=f"{store} · {event.category or 'uncategorized'}")
    return embed


def digest_embed(events: Sequence[RadarEvent], *, limit: int = 25) -> discord.Embed:
    embed = discord.Embed(title=f"📡 Radar digest · {len(events)} updates", color=0x5865F2)
    lines = []
    for event in events[:limit]:
        emoji, label, _ = style(event)
        name = event.sku or event.product_name or "item"
        detail = (
            f"{event.old_value or '—'} → {event.new_value or '—'}"
            if event.kind in (EventKind.PRICE_CHANGE, EventKind.RESTOCK_ETA)
            else format_cents(event.price_cents, event.currency)
        )
        region = f" {event.region.upper()}" if event.region else ""
        link = f"[{name}]({event.url})" if event.url else name
        lines.append(f"{emoji} {label} · {link}{region} · {detail}")
    if len(events) > limit:
        lines.append(f"…and {len(events) - limit} more (see `/radar`)")
    embed.description = "\n".join(lines)[:4096]
    embed.timestamp = events[-1].occurred_at if events else None
    return embed
