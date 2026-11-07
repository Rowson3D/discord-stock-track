"""Read/write helpers behind the Discord commands (free of Discord types, so testable)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import (
    Event,
    Listing,
    MarketComp,
    Product,
    StockObservation,
    Valuation,
    Variant,
    Watch,
)
from tracker.domain import StockStatus


@dataclass(frozen=True, slots=True)
class VariantMatch:
    variant_id: int
    sku: str
    product_name: str
    pack_size: int

    @property
    def label(self) -> str:
        pack = f" ({self.pack_size}-pack)" if self.pack_size > 1 else ""
        return f"{self.sku} · {self.product_name}{pack}"[:100]


@dataclass(frozen=True, slots=True)
class ListingRow:
    listing: Listing
    variant: Variant
    product: Product
    max_price_cents: int | None = None


async def search_variants(session: AsyncSession, query: str, limit: int = 25) -> list[VariantMatch]:
    """SKU prefix matches first, then SKU/name substring matches."""
    text = query.strip().lower()
    stmt = select(Variant.id, Variant.sku, Product.name, Variant.pack_size).join(Product)
    if text:
        like = f"%{text}%"
        stmt = stmt.where(or_(Variant.sku_key.like(like), func.lower(Product.name).like(like)))
    stmt = stmt.order_by(
        Variant.sku_key.like(f"{text}%").desc(), func.length(Variant.sku_key), Variant.sku_key
    ).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [VariantMatch(v_id, sku, name, pack) for v_id, sku, name, pack in rows]


async def resolve_variant(session: AsyncSession, text: str) -> Variant | None:
    """Exact SKU (case-insensitive), else the best search match."""
    exact = await session.scalar(select(Variant).where(Variant.sku_key == text.strip().lower()))
    if exact is not None:
        return exact
    matches = await search_variants(session, text, limit=1)
    return await session.get(Variant, matches[0].variant_id) if matches else None


async def variant_listings(
    session: AsyncSession, variant_id: int, region: str | None = None
) -> list[ListingRow]:
    stmt = (
        select(Listing, Variant, Product)
        .join(Variant, Listing.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(Variant.id == variant_id)
        .order_by(Listing.source, Listing.region)
    )
    if region:
        stmt = stmt.where(Listing.region == region)
    return [ListingRow(*row) for row in (await session.execute(stmt)).all()]


async def listings_for_url(session: AsyncSession, url: str) -> list[ListingRow]:
    stmt = (
        select(Listing, Variant, Product)
        .join(Variant, Listing.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(or_(Listing.url == url, Listing.external_id == url))
        .order_by(Variant.pack_size, Variant.sku)
    )
    return [ListingRow(*row) for row in (await session.execute(stmt)).all()]


async def unifi_listings_for_slug(
    session: AsyncSession, region: str, slug: str
) -> list[ListingRow]:
    stmt = (
        select(Listing, Variant, Product)
        .join(Variant, Listing.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(
            Listing.source == "unifi",
            Listing.region == region,
            Listing.url.like(f"%/{region}/%/products/{slug}"),
        )
        .order_by(Variant.pack_size, Variant.sku)
    )
    return [ListingRow(*row) for row in (await session.execute(stmt)).all()]


async def add_watches(
    session: AsyncSession, user_id: str, listing_ids: list[int], max_price_cents: int | None
) -> int:
    """Create or update watches. Returns how many were newly created."""
    existing = {
        w.listing_id: w
        for w in await session.scalars(
            select(Watch).where(Watch.discord_user_id == user_id, Watch.listing_id.in_(listing_ids))
        )
    }
    created = 0
    for listing_id in listing_ids:
        if listing_id in existing:
            existing[listing_id].max_price_cents = max_price_cents
        else:
            session.add(
                Watch(
                    discord_user_id=user_id, listing_id=listing_id, max_price_cents=max_price_cents
                )
            )
            created += 1
    await session.flush()
    return created


async def remove_watches(session: AsyncSession, user_id: str, listing_ids: list[int]) -> int:
    result = await session.execute(
        delete(Watch).where(Watch.discord_user_id == user_id, Watch.listing_id.in_(listing_ids))
    )
    return result.rowcount or 0


async def toggle_watch(session: AsyncSession, user_id: str, listing_id: int) -> bool:
    """Returns True if the user is now watching."""
    if await remove_watches(session, user_id, [listing_id]):
        return False
    await add_watches(session, user_id, [listing_id], None)
    return True


async def user_watches(session: AsyncSession, user_id: str) -> list[ListingRow]:
    stmt = (
        select(Listing, Variant, Product, Watch.max_price_cents)
        .join(Watch, Watch.listing_id == Listing.id)
        .join(Variant, Listing.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(Watch.discord_user_id == user_id)
        .order_by(Variant.sku, Listing.source, Listing.region)
    )
    return [ListingRow(*row) for row in (await session.execute(stmt)).all()]


async def set_subscribed(session: AsyncSession, user_id: str, subscribed: bool) -> bool:
    """Global 'ping me for every important alert'. Returns True if anything changed."""
    existing = await session.scalar(
        select(Watch).where(Watch.discord_user_id == user_id, Watch.listing_id.is_(None))
    )
    if subscribed and existing is None:
        session.add(Watch(discord_user_id=user_id, listing_id=None))
        return True
    if not subscribed and existing is not None:
        await session.delete(existing)
        return True
    return False


async def recent_observations(
    session: AsyncSession, listing_id: int, limit: int = 5
) -> list[StockObservation]:
    return list(
        await session.scalars(
            select(StockObservation)
            .where(StockObservation.listing_id == listing_id)
            .order_by(StockObservation.observed_at.desc())
            .limit(limit)
        )
    )


async def upcoming_restocks(session: AsyncSession, today: date, days: int) -> list[ListingRow]:
    stmt = (
        select(Listing, Variant, Product)
        .join(Variant, Listing.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(
            Listing.restock_eta.is_not(None),
            Listing.restock_eta <= today + timedelta(days=days),
            Listing.status != StockStatus.IN_STOCK.value,
            Listing.delisted_at.is_(None),
        )
        .order_by(Listing.restock_eta, Variant.sku)
    )
    return [ListingRow(*row) for row in (await session.execute(stmt)).all()]


@dataclass(slots=True)
class TrackerStats:
    status_counts: dict[tuple[str, str], int]  # (region, status) -> count
    last_checked: dict[str, datetime | None]  # region -> newest last_checked_at
    watches: int
    subscribers: int
    pending_events: int
    events_24h: int


async def tracker_stats(session: AsyncSession, now: datetime) -> TrackerStats:
    rows = await session.execute(
        select(Listing.region, Listing.status, func.count(), func.max(Listing.last_checked_at))
        .where(Listing.delisted_at.is_(None))
        .group_by(Listing.region, Listing.status)
    )
    counts: dict[tuple[str, str], int] = {}
    last: dict[str, datetime | None] = {}
    for region, status, count, newest in rows.all():
        counts[(region, status)] = count
        if newest and (last.get(region) is None or newest > last[region]):
            last[region] = newest
    watches = await session.scalar(select(func.count()).where(Watch.listing_id.is_not(None)))
    subscribers = await session.scalar(select(func.count()).where(Watch.listing_id.is_(None)))
    pending = await session.scalar(select(func.count()).where(Event.notified_at.is_(None)))
    recent = await session.scalar(
        select(func.count()).where(Event.occurred_at >= now - timedelta(hours=24))
    )
    return TrackerStats(counts, last, watches or 0, subscribers or 0, pending or 0, recent or 0)


async def set_priority(session: AsyncSession, variant_id: int, priority: str) -> int:
    listings = await session.scalars(select(Listing).where(Listing.variant_id == variant_id))
    changed = 0
    for listing in listings:
        listing.priority = priority
        changed += 1
    return changed


# ── market ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OpportunityRow:
    valuation: Valuation
    variant: Variant
    product: Product
    listing: Listing | None


async def opportunities(
    session: AsyncSession,
    *,
    limit: int = 15,
    min_profit_cents: int | None = None,
    available_only: bool = False,
    category: str | None = None,
) -> list[OpportunityRow]:
    stmt = (
        select(Valuation, Variant, Product, Listing)
        .join(Variant, Valuation.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .outerjoin(Listing, Valuation.listing_id == Listing.id)
        .where(Valuation.net_cents.is_not(None))
        .order_by(Valuation.score.desc(), Valuation.net_cents.desc())
        .limit(limit)
    )
    if min_profit_cents is not None:
        stmt = stmt.where(Valuation.net_cents >= min_profit_cents)
    if available_only:
        stmt = stmt.where(Valuation.available.is_(True))
    if category:
        stmt = stmt.where(func.lower(Product.category).like(f"%{category.lower()}%"))
    return [OpportunityRow(*row) for row in (await session.execute(stmt)).all()]


async def variant_comps(
    session: AsyncSession, variant_id: int, *, include_excluded: bool = False, limit: int = 15
) -> list[MarketComp]:
    stmt = (
        select(MarketComp)
        .where(MarketComp.variant_id == variant_id)
        .order_by(MarketComp.sold.desc(), MarketComp.observed_at.desc())
        .limit(limit)
    )
    if not include_excluded:
        stmt = stmt.where(MarketComp.excluded_reason.is_(None))
    return list(await session.scalars(stmt))
