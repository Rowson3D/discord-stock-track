"""Collect resale comps, value variants, and raise `deal` events.

eBay compliance: the eBay app declares "I do not persist eBay data". eBay results are matched
and summarized in memory; only aggregate numbers (`EbayStats`) are stored. Manual comps are
the user's own data and are stored as-is.

Collection is rate-conscious (eBay's default Browse quota is 5,000 calls/day): one variant
per tick, hot variants refreshed every few hours and everything else every few days, and
cheap accessories skipped unless someone watches them.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import (
    EbayStats,
    EbayStatsHistory,
    Event,
    Listing,
    MarketComp,
    Product,
    Valuation,
    Variant,
    Watch,
)
from tracker.domain import EventKind, Priority, StockStatus, format_cents
from tracker.history import scarcity_stats
from tracker.market.ebay import EbayClient, EbayError, EbayItem
from tracker.market.matching import MatchResult, VariantMatcher, base_sku
from tracker.market.valuation import (
    Comp,
    MarketStats,
    ValuationResult,
    compute_valuation,
    is_deal,
    summarize,
)
from tracker.outbox import Dispatcher
from tracker.settings import MarketSettings

if TYPE_CHECKING:
    from tracker.market.scan import ScanReport

logger = logging.getLogger(__name__)

SOURCE_MANUAL = "manual"
TIER_HOT, TIER_DAILY, TIER_NORMAL = 0, 1, 2
STAT_FIELDS = (
    "sold_count",
    "sold_30d",
    "sold_median_cents",
    "sold_p25_cents",
    "sold_p75_cents",
    "active_count",
    "active_median_cents",
    "active_p25_cents",
    "active_low_cents",
    "active_listings",
)


@dataclass(slots=True)
class CollectReport:
    variant_id: int
    sku: str
    query: str
    fetched: int = 0
    matched: int = 0
    excluded: int = 0
    valuation: ValuationResult | None = None
    # Live results for display only; never persisted.
    items: list[tuple[EbayItem, MatchResult]] = field(default_factory=list)


def search_query(sku: str, pack_size: int) -> str:
    query = "ubiquiti " + base_sku(sku, pack_size).replace("-", " ")
    return f"{query} {pack_size} pack" if pack_size > 1 else query


async def retail_listing(session: AsyncSession, variant_id: int) -> Listing | None:
    """The listing we'd buy from: US/USD first, available first, then cheapest."""
    listings = (
        await session.scalars(select(Listing).where(Listing.variant_id == variant_id))
    ).all()
    usd = [
        item
        for item in listings
        if item.currency == "USD" and item.price_cents and item.delisted_at is None
    ]
    if not usd:
        return None
    return min(
        usd,
        key=lambda item: (
            item.region != "us",
            not StockStatus(item.status).is_available,
            item.price_cents,
        ),
    )


class MarketService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ebay: EbayClient | None,
        settings: MarketSettings,
        dispatcher: Dispatcher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._ebay = ebay
        self._settings = settings
        self._dispatcher = dispatcher
        self._clock = clock
        self._scan_lock = asyncio.Lock()

    @property
    def collecting(self) -> bool:
        return self._ebay is not None

    # ── collection ────────────────────────────────────────────────────────

    async def due_variants(self, limit: int) -> list[int]:
        """Variants whose eBay data is stale for their tier, most important first.

        Tiers: hot (watched, high priority, Early Access, coming soon, restock within a week)
        every `refresh_hot_hours`; end-of-life candidates (sold out, withdrawn from a region,
        or delisted) every `refresh_daily_hours`; everything else every `refresh_hours`.
        """
        now = self._clock()
        cutoffs = {
            TIER_HOT: now - timedelta(hours=self._settings.refresh_hot_hours),
            TIER_DAILY: now - timedelta(hours=self._settings.refresh_daily_hours),
            TIER_NORMAL: now - timedelta(hours=self._settings.refresh_hours),
        }
        soon = now.date() + timedelta(days=7)
        watched = exists().where(Watch.listing_id == Listing.id)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        Variant.id,
                        Variant.comps_checked_at,
                        Variant.pack_size,
                        Product.category,
                        Listing.price_cents,
                        Listing.currency,
                        Listing.priority,
                        Listing.is_early_access,
                        Listing.status,
                        Listing.restock_eta,
                        Listing.delisted_at,
                        watched,
                    )
                    .join(Listing, Listing.variant_id == Variant.id)
                    .join(Product, Variant.product_id == Product.id)
                )
            ).all()

        # Aggregate per variant across its listings (regions/retailers).
        tier: dict[int, int] = {}
        watched_any: dict[int, bool] = {}
        top_price: dict[int, int] = {}
        checked_at: dict[int, datetime | None] = {}
        accessory: dict[int, bool] = {}
        for (
            vid,
            checked,
            pack_size,
            category,
            price,
            currency,
            priority,
            ea,
            status,
            eta,
            delisted,
            is_watched,
        ) in rows:
            if delisted is None and (
                is_watched
                or priority == Priority.HIGH.value
                or ea
                or status == StockStatus.COMING_SOON.value
                or (isinstance(eta, date) and eta <= soon)
            ):
                listing_tier = TIER_HOT
            elif delisted is not None or not StockStatus(status).is_available:
                listing_tier = TIER_DAILY
            else:
                listing_tier = TIER_NORMAL
            tier[vid] = min(tier.get(vid, TIER_NORMAL), listing_tier)
            watched_any[vid] = watched_any.get(vid, False) or bool(is_watched)
            # Per-unit price: a 10-pack of $5 cables is not a $50 item.
            usd_price = (price or 0) // max(1, pack_size) if currency == "USD" else 0
            top_price[vid] = max(top_price.get(vid, 0), usd_price)
            checked_at[vid] = checked
            accessory[vid] = (category or "").startswith("accessories")

        due = [
            (vid, tier[vid], checked_at[vid], top_price[vid])
            for vid in tier
            # Skip accessories and cheap items unless someone is watching them.
            if (
                watched_any[vid]
                or (top_price[vid] >= self._settings.min_retail_cents and not accessory[vid])
            )
            and (checked_at[vid] is None or checked_at[vid] < cutoffs[tier[vid]])
        ]
        # Most important tier first, never-checked first, stalest, then most expensive.
        due.sort(key=lambda d: (d[1], d[2] is not None, d[2] or now, -d[3]))
        return [vid for vid, *_ in due[:limit]]

    async def collect_due(self) -> int:
        if self._ebay is None:
            return 0
        collected = 0
        for variant_id in await self.due_variants(self._settings.collect_per_tick):
            try:
                await self.collect(variant_id)
                collected += 1
            except EbayError as exc:
                logger.warning("eBay comp collection failed: %s", exc)
                if exc.status == 429:
                    break  # Over quota: wait for the next tick.
        return collected

    async def collect(self, variant_id: int) -> CollectReport:
        """Fetch eBay results, match them in memory, and store only aggregate stats.

        Listing-level eBay data (IDs, titles, URLs, individual prices) is never written to the
        database; it's returned on the report for live display and then discarded.
        """
        if self._ebay is None:
            raise EbayError("eBay is not configured (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
        now = self._clock()
        async with self._sessions() as session:
            variant = await session.get(Variant, variant_id)
            if variant is None:
                raise ValueError(f"unknown variant {variant_id}")
            sku, pack = variant.sku, variant.pack_size
            product = await session.get(Product, variant.product_id)
            product_name = product.name if product else None
            catalog = list(await session.scalars(select(Variant.sku)))
            retail = await retail_listing(session, variant_id)
            retail_cents = retail.price_cents if retail else None

        query = search_query(sku, pack)
        floor = int(retail_cents * 0.3) if retail_cents else None
        active = await self._ebay.search_active(query, min_price_cents=floor)
        sold = await self._ebay.search_sold(query, min_price_cents=floor)

        report = CollectReport(variant_id, sku, query, fetched=len(active) + len(sold))
        matcher = VariantMatcher(sku, pack, catalog, product_name)
        comps: list[Comp] = []
        for item in [*active, *sold]:
            result = matcher.match(item.title, item.condition_id)
            if result.reason == "sku not in title":
                continue  # Search noise.
            report.items.append((item, result))
            if not result.matched:
                report.excluded += 1
                continue
            report.matched += 1
            comps.append(
                Comp(
                    source="ebay_sold" if item.sold else "ebay_active",
                    price_cents=item.price_cents,
                    shipping_cents=item.shipping_cents,
                    quantity=result.quantity,
                    condition=result.condition or "unknown",
                    sold=item.sold,
                    sold_at=item.sold_at,
                    observed_at=now,
                    match_score=result.score,
                    seller=item.seller,
                )
            )
        stats = summarize(comps, retail_cents, self._settings, now)

        async with self._sessions() as session, session.begin():
            row = await session.get(EbayStats, variant_id)
            if row is None:
                row = EbayStats(variant_id=variant_id)
                session.add(row)
            row.fetched_at = now
            for name in STAT_FIELDS:
                setattr(row, name, getattr(stats, name))
            row.matched, row.excluded = report.matched, report.excluded
            await self._record_history(session, variant_id, stats, retail_cents, now)
            stored = await session.get(Variant, variant_id)
            assert stored is not None
            stored.comps_checked_at = now

        report.valuation = (await self.revalue([variant_id])).get(variant_id)
        logger.info(
            "eBay comps for %s (%r): %d fetched, %d matched, %d excluded",
            sku,
            query,
            report.fetched,
            report.matched,
            report.excluded,
        )
        return report

    @staticmethod
    async def _record_history(
        session: AsyncSession,
        variant_id: int,
        stats: MarketStats,
        retail_cents: int | None,
        now: datetime,
    ) -> None:
        """Upsert today's aggregate row (the latest collection of the day wins)."""
        day = now.astimezone(UTC).date()
        row = await session.scalar(
            select(EbayStatsHistory).where(
                EbayStatsHistory.variant_id == variant_id, EbayStatsHistory.day == day
            )
        )
        if row is None:
            row = EbayStatsHistory(variant_id=variant_id, day=day)
            session.add(row)
        row.fetched_at = now
        row.retail_cents = retail_cents
        for name in STAT_FIELDS:
            setattr(row, name, getattr(stats, name))

    async def add_manual_comp(
        self,
        variant_id: int,
        price_cents: int,
        *,
        shipping_cents: int | None = None,
        condition: str = "new",
        quantity: int = 1,
        sold_at: datetime | None = None,
        url: str | None = None,
        note: str | None = None,
        created_by: str | None = None,
    ) -> ValuationResult | None:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            session.add(
                MarketComp(
                    variant_id=variant_id,
                    source=SOURCE_MANUAL,
                    external_id=uuid.uuid4().hex,
                    title=note or "manual comp",
                    url=url,
                    price_cents=price_cents,
                    shipping_cents=shipping_cents,
                    currency="USD",
                    condition=condition,
                    quantity=max(1, quantity),
                    sold=True,
                    sold_at=sold_at or now,
                    observed_at=now,
                    match_score=1.0,
                    note=note,
                    created_by=created_by,
                )
            )
        return (await self.revalue([variant_id])).get(variant_id)

    # ── on-demand scan ────────────────────────────────────────────────────

    @property
    def scanning(self) -> bool:
        return self._scan_lock.locked()

    async def scan(self, *, limit: int = 25, min_retail_cents: int = 10000) -> ScanReport:
        """Read-only live profit scan (nothing stored, no alerts). One scan at a time."""
        from tracker.market.scan import run_scan  # Local import: scan imports this module.

        if self._ebay is None:
            raise EbayError("eBay is not configured (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
        async with self._scan_lock:
            return await run_scan(
                self._sessions,
                self._ebay,
                self._settings,
                limit=limit,
                min_retail_cents=min_retail_cents,
                now=self._clock(),
            )

    # ── valuation ─────────────────────────────────────────────────────────

    async def revalue(self, variant_ids: Sequence[int] | None = None) -> dict[int, ValuationResult]:
        """Recompute valuations from stored eBay stats + manual comps. Emits `deal` events.

        With `variant_ids=None`, every variant that has eBay stats or manual comps is valued.
        """
        now = self._clock()
        active_cutoff = now - timedelta(hours=self._settings.active_max_age_hours)
        results: dict[int, ValuationResult] = {}
        events = 0
        async with self._sessions() as session, session.begin():
            manual_stmt = select(MarketComp).where(MarketComp.source == SOURCE_MANUAL)
            stats_stmt = select(EbayStats)
            if variant_ids is not None:
                manual_stmt = manual_stmt.where(MarketComp.variant_id.in_(list(variant_ids)))
                stats_stmt = stats_stmt.where(EbayStats.variant_id.in_(list(variant_ids)))
            manual: dict[int, list[Comp]] = {}
            for row in await session.scalars(manual_stmt):
                manual.setdefault(row.variant_id, []).append(
                    Comp(
                        source=row.source,
                        price_cents=row.price_cents,
                        shipping_cents=row.shipping_cents,
                        quantity=row.quantity,
                        condition=row.condition,
                        sold=True,
                        sold_at=row.sold_at,
                        observed_at=row.observed_at,
                    )
                )
            ebay: dict[int, MarketStats] = {}
            for row in await session.scalars(stats_stmt):
                stats = MarketStats(**{name: getattr(row, name) for name in STAT_FIELDS})
                ebay[row.variant_id] = (
                    stats if row.fetched_at >= active_cutoff else stats.without_active()
                )

            targets = list(variant_ids) if variant_ids is not None else sorted({*manual, *ebay})
            for variant_id in targets:
                listing = await retail_listing(session, variant_id)
                retail_cents = listing.price_cents if listing else None
                available = bool(listing and StockStatus(listing.status).is_available)
                manual_stats = summarize(
                    manual.get(variant_id, []), retail_cents, self._settings, now
                )
                scarcity = (
                    await scarcity_stats(
                        session,
                        variant_id,
                        listing.id,
                        now=now,
                        window_days=self._settings.scarcity_window_days,
                    )
                    if listing is not None
                    else None
                )
                result = compute_valuation(
                    retail_cents,
                    ebay.get(variant_id),
                    manual_stats,
                    self._settings,
                    available=available,
                    scarcity=scarcity,
                )
                results[variant_id] = result
                events += await self._store(session, variant_id, listing, available, result, now)
        if events:
            await self._dispatcher.dispatch()
        return results

    async def revalue_all(self) -> int:
        return len(await self.revalue(None))

    async def _store(
        self,
        session: AsyncSession,
        variant_id: int,
        listing: Listing | None,
        available: bool,
        result: ValuationResult,
        now: datetime,
    ) -> int:
        valuation = await session.get(Valuation, variant_id)
        if valuation is None:
            valuation = Valuation(variant_id=variant_id, deal_active=False)
            session.add(valuation)
        valuation.computed_at = now
        valuation.listing_id = listing.id if listing else None
        valuation.currency = "USD"
        valuation.available = available
        for name in (
            "retail_cents", "basis", "resale_cents", "fees_cents", "shipping_cents",
            "cost_cents", "net_cents", "roi", "sold_count", "sold_30d", "active_count",
            "manual_count", "sold_median_cents", "sold_p25_cents", "sold_p75_cents",
            "active_median_cents", "active_low_cents", "sell_through", "confidence", "score",
            "scarcity_days", "scarcity_resale_cents", "scarcity_net_cents", "scarcity_roi",
            "flip_basis", "flip_net_cents", "flip_roi", "flip_confidence",
        ):  # fmt: skip
            setattr(valuation, name, getattr(result, name))

        deal_now = is_deal(result, available, self._settings)
        fired = 0
        if deal_now and not valuation.deal_active and listing is not None:
            assert result.flip_net_cents is not None
            assert result.flip_roi is not None
            evidence = {
                "sold": "eBay sold prices",
                "manual": "your recorded sales",
                "scarcity": (
                    f"sold-out resale ~{format_cents(result.scarcity_resale_cents)} "
                    f"over {result.scarcity_days} days"
                ),
            }.get(result.flip_basis or "", result.flip_basis or "")
            session.add(
                Event(
                    listing=listing,
                    kind=EventKind.DEAL.value,
                    occurred_at=now,
                    old_value=None,
                    new_value=(
                        f"net {format_cents(result.flip_net_cents)} · "
                        f"ROI {result.flip_roi:.0%} · {evidence}"
                    ),
                )
            )
            fired = 1
        valuation.deal_active = deal_now
        return fired
