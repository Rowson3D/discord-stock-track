"""Watch preferences and rules: who gets pinged for which alerts, shared by every front end.

- **Alert keys** are the user-facing alert types. They're mostly event kinds, plus
  `price_drop` (a price change downward). `back_in_stock` also covers `relisted`.
- **Watches** can pick their alert types (`kinds`), set a target price, or be muted.
  A watch with no `kinds` keeps the server's mention defaults.
- **Rules** watch products that match criteria (text, category, region, price, credible
  profit), including products that don't exist yet.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Listing, Product, Variant, Watch, WatchRule
from tracker.domain import EventKind, SourceKey
from tracker.outbox import RadarEvent
from tracker.queries import (
    ListingRow,
    listings_for_url,
    resolve_variant,
    unifi_listings_for_slug,
    variant_listings,
)
from tracker.sources.web import detect_source, parse_money

ALERT_KEYS: dict[str, str] = {
    "back_in_stock": "Back in stock",
    "early_access": "Early Access",
    "price_drop": "Price drop",
    "deal": "Deal",
    "new_listing": "New listing",
    "restock_eta": "Restock date",
    "sold_out": "Sold out",
}
RULE_DEFAULT_KINDS = frozenset(
    {"back_in_stock", "early_access", "new_listing", "deal", "price_drop"}
)

_KIND_TO_KEY = {
    EventKind.BACK_IN_STOCK: "back_in_stock",
    EventKind.RELISTED: "back_in_stock",
    EventKind.EARLY_ACCESS: "early_access",
    EventKind.DEAL: "deal",
    EventKind.NEW_LISTING: "new_listing",
    EventKind.RESTOCK_ETA: "restock_eta",
    EventKind.SOLD_OUT: "sold_out",
}
UNIFI_URL_RE = re.compile(r"/(?P<region>[a-z]{2,5})/[a-z]{2}/products/(?P<slug>[^/?#]+)")


def is_price_drop(event: RadarEvent) -> bool:
    if event.kind is not EventKind.PRICE_CHANGE:
        return False
    old, new = parse_money(event.old_value), parse_money(event.new_value)
    return old is not None and new is not None and new < old


def alert_key(event: RadarEvent) -> str | None:
    """The alert type a user would subscribe to for this event (None: never pings)."""
    if is_price_drop(event):
        return "price_drop"
    return _KIND_TO_KEY.get(event.kind)


def parse_kinds(value: str | None) -> frozenset[str] | None:
    if value is None:
        return None
    return frozenset(k for k in (part.strip() for part in value.split(",")) if k in ALERT_KEYS)


def format_kinds(kinds: Iterable[str] | None) -> str | None:
    """Validate and store alert types. None keeps defaults; an empty list means 'none'."""
    if kinds is None:
        return None
    chosen = sorted(set(kinds))
    unknown = [k for k in chosen if k not in ALERT_KEYS]
    if unknown:
        raise ValueError(f"unknown alert type(s): {', '.join(unknown)}")
    return ",".join(chosen)


def _muted(muted_until: datetime | None, now: datetime) -> bool:
    if muted_until is None:
        return False
    if muted_until.tzinfo is None:
        muted_until = muted_until.replace(tzinfo=UTC)
    return muted_until > now


def _price_ok(event: RadarEvent, max_price_cents: int | None) -> bool:
    return max_price_cents is None or (
        event.price_cents is not None and event.price_cents <= max_price_cents
    )


def watch_pings(
    watch: Watch, event: RadarEvent, server_kinds: frozenset[EventKind], now: datetime
) -> bool:
    """Does this watch ping its owner for this event?"""
    if _muted(watch.muted_until, now) or not _price_ok(event, watch.max_price_cents):
        return False
    key = alert_key(event)
    chosen = parse_kinds(watch.kinds)
    if chosen is None:  # Server defaults, plus price drops on anything you watch.
        return event.kind in server_kinds or key == "price_drop"
    return key is not None and key in chosen


def rule_matches(rule: WatchRule, event: RadarEvent, now: datetime) -> bool:
    if not rule.enabled or _muted(rule.muted_until, now):
        return False
    key = alert_key(event)
    if key is None or key not in (parse_kinds(rule.kinds) or RULE_DEFAULT_KINDS):
        return False
    if rule.query:
        needle = rule.query.lower()
        haystack = f"{event.sku or ''} {event.product_name or ''}".lower()
        if needle not in haystack:
            return False
    if rule.category and rule.category.lower() not in (event.category or "").lower():
        return False
    if rule.regions:
        regions = {r.strip().lower() for r in rule.regions.split(",") if r.strip()}
        if (event.region or "").lower() not in regions:
            return False
    if not _price_ok(event, rule.max_price_cents):
        return False
    # Credible profit only (alerts carry flip values), so rules never chase asking prices.
    return rule.min_profit_cents is None or (
        event.net_cents is not None and event.net_cents >= rule.min_profit_cents
    )


# ── resolving what to watch ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Resolved:
    rows: list[ListingRow]
    error: str | None = None


PollUnifi = Callable[[str, str], Awaitable[object]]
AddRetailUrl = Callable[[str, SourceKey], Awaitable[object]]


async def resolve_listings(
    sessions: async_sessionmaker[AsyncSession],
    text: str,
    region: str | None = None,
    *,
    poll_unifi: PollUnifi | None = None,
    add_retail_url: AddRetailUrl | None = None,
) -> Resolved:
    """A SKU, product name, or store URL → listings (fetching unknown URLs when possible)."""
    text = text.strip()
    if not text:
        return Resolved([], "Enter a SKU, product name, or store link.")
    if text.lower().startswith(("http://", "https://")):
        source = detect_source(text)
        if source is None:
            return Resolved([], "That URL isn't from a supported store.")
        if source is SourceKey.UNIFI:
            match = UNIFI_URL_RE.search(text)
            if not match:
                return Resolved([], "That doesn't look like a UniFi store product URL.")
            url_region, slug = match["region"], match["slug"]
            async with sessions() as session:
                rows = await unifi_listings_for_slug(session, url_region, slug)
            if not rows and poll_unifi is not None:
                await poll_unifi(url_region, slug)
                async with sessions() as session:
                    rows = await unifi_listings_for_slug(session, url_region, slug)
            return Resolved(rows, None if rows else "Couldn't load that product page.")
        if add_retail_url is None:
            return Resolved([], "Adding new store links isn't available here.")
        await add_retail_url(text, source)
        async with sessions() as session:
            rows = await listings_for_url(session, text)
        return Resolved(rows, None if rows else "Couldn't load that product page.")

    async with sessions() as session:
        variant = await resolve_variant(session, text)
        if variant is None:
            return Resolved([], f"No product matches `{text}`.")
        rows = await variant_listings(session, variant.id, region)
    if not rows:
        return Resolved([], f"`{variant.sku}` isn't listed in region `{region}`.")
    return Resolved(rows)


def now_utc() -> datetime:
    return datetime.now(UTC)


# ── a user's own watches (Discord management) ───────────────────────────


@dataclass(frozen=True, slots=True)
class WatchRow:
    watch: Watch
    listing: Listing
    variant: Variant
    product: Product


async def user_watch_rows(session: AsyncSession, user_id: str) -> list[WatchRow]:
    rows = await session.execute(
        select(Watch, Listing, Variant, Product)
        .join(Listing, Watch.listing_id == Listing.id)
        .join(Variant, Listing.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(Watch.discord_user_id == user_id)
        .order_by(Variant.sku, Listing.source, Listing.region)
    )
    return [WatchRow(*row) for row in rows.all()]


async def mute_watches(
    session: AsyncSession, user_id: str, watch_ids: list[int], until: datetime | None
) -> int:
    """Mute (or unmute, with None) the user's own watches. Returns how many changed."""
    count = 0
    for watch in await session.scalars(
        select(Watch).where(Watch.discord_user_id == user_id, Watch.id.in_(watch_ids))
    ):
        watch.muted_until = until
        count += 1
    return count


async def remove_watch_ids(session: AsyncSession, user_id: str, watch_ids: list[int]) -> int:
    result = await session.execute(
        delete(Watch).where(Watch.discord_user_id == user_id, Watch.id.in_(watch_ids))
    )
    return result.rowcount or 0
