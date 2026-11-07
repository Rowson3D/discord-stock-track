"""Collect resale comps, value variants, and raise `deal` events.

Collection is rate-conscious (eBay's default Browse quota is 5,000 calls/day): one variant
per tick, hot variants refreshed every few hours and everything else every few days, and
cheap accessories skipped unless someone watches them.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Event, Listing, MarketComp, Valuation, Variant, Watch
from tracker.domain import EventKind, Priority, StockStatus, format_cents
from tracker.market.ebay import EbayClient, EbayError, EbayItem
from tracker.market.matching import MatchResult, VariantMatcher, base_sku
from tracker.market.valuation import Comp, ValuationResult, compute_valuation, is_deal
from tracker.outbox import Dispatcher
from tracker.settings import MarketSettings

logger = logging.getLogger(__name__)

SOURCE_SOLD, SOURCE_ACTIVE, SOURCE_MANUAL = "ebay_sold", "ebay_active", "manual"


@dataclass(slots=True)
class CollectReport:
    variant_id: int
    sku: str
    query: str
    fetched: int = 0
    matched: int = 0
    excluded: int = 0
    valuation: ValuationResult | None = None


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

    @property
    def collecting(self) -> bool:
        return self._ebay is not None

    # ── collection ────────────────────────────────────────────────────────

    async def due_variants(self, limit: int) -> list[int]:
        now = self._clock()
        hot_cutoff = now - timedelta(hours=self._settings.refresh_hot_hours)
        cutoff = now - timedelta(hours=self._settings.refresh_hours)
        soon = now.date() + timedelta(days=7)
        watched = exists().where(Watch.listing_id == Listing.id)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        Variant.id,
                        Variant.comps_checked_at,
                        Listing.price_cents,
                        Listing.currency,
                        Listing.priority,
                        Listing.is_early_access,
                        Listing.status,
                        Listing.restock_eta,
                        watched,
                    )
                    .join(Listing, Listing.variant_id == Variant.id)
                    .where(Listing.delisted_at.is_(None))
                )
            ).all()

        # Aggregate per variant across its listings (regions/retailers).
        hot: dict[int, bool] = {}
        watched_any: dict[int, bool] = {}
        top_price: dict[int, int] = {}
        checked_at: dict[int, datetime | None] = {}
        for vid, checked, price, currency, priority, ea, status, eta, is_watched in rows:
            hot[vid] = hot.get(vid, False) or bool(
                is_watched
                or priority == Priority.HIGH.value
                or ea
                or status == StockStatus.COMING_SOON.value
                or (isinstance(eta, date) and eta <= soon)
            )
            watched_any[vid] = watched_any.get(vid, False) or bool(is_watched)
            usd_price = (price or 0) if currency == "USD" else 0
            top_price[vid] = max(top_price.get(vid, 0), usd_price)
            checked_at[vid] = checked

        due = [
            (vid, hot[vid], checked_at[vid], top_price[vid])
            for vid in hot
            # Skip cheap accessories unless someone is watching them.
            if (watched_any[vid] or top_price[vid] >= self._settings.min_retail_cents)
            and (
                checked_at[vid] is None or checked_at[vid] < (hot_cutoff if hot[vid] else cutoff)  # type: ignore[operator]
            )
        ]
        # Hot first, never-checked first, then the most expensive (most profit at stake).
        due.sort(key=lambda d: (not d[1], d[2] is not None, d[2] or now, -d[3]))
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
        if self._ebay is None:
            raise EbayError("eBay is not configured (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
        now = self._clock()
        async with self._sessions() as session:
            variant = await session.get(Variant, variant_id)
            if variant is None:
                raise ValueError(f"unknown variant {variant_id}")
            sku, pack = variant.sku, variant.pack_size
            catalog = list(await session.scalars(select(Variant.sku)))
            retail = await retail_listing(session, variant_id)
            retail_cents = retail.price_cents if retail else None

        query = search_query(sku, pack)
        floor = int(retail_cents * 0.3) if retail_cents else None
        active = await self._ebay.search_active(query, min_price_cents=floor)
        sold = await self._ebay.search_sold(query, min_price_cents=floor)

        report = CollectReport(variant_id, sku, query, fetched=len(active) + len(sold))
        matcher = VariantMatcher(sku, pack, catalog)
        async with self._sessions() as session, session.begin():
            for item in [*active, *sold]:
                result = matcher.match(item.title, item.condition_id)
                if result.reason == "sku not in title":
                    continue  # Search noise, not worth storing.
                await self._upsert_comp(session, variant_id, item, result, now)
                if result.matched:
                    report.matched += 1
                else:
                    report.excluded += 1
            stored = await session.get(Variant, variant_id)
            assert stored is not None
            stored.comps_checked_at = now

        report.valuation = (await self.revalue([variant_id])).get(variant_id)
        logger.info(
            "Comps for %s (%r): %d fetched, %d matched, %d excluded",
            sku,
            query,
            report.fetched,
            report.matched,
            report.excluded,
        )
        return report

    @staticmethod
    async def _upsert_comp(
        session: AsyncSession,
        variant_id: int,
        item: EbayItem,
        result: MatchResult,
        now: datetime,
    ) -> None:
        source = SOURCE_SOLD if item.sold else SOURCE_ACTIVE
        comp = await session.scalar(
            select(MarketComp).where(
                MarketComp.source == source, MarketComp.external_id == item.item_id
            )
        )
        if comp is None:
            comp = MarketComp(source=source, external_id=item.item_id, variant_id=variant_id)
            session.add(comp)
        comp.variant_id = variant_id
        comp.title = item.title
        comp.url = item.url
        comp.price_cents = item.price_cents
        comp.shipping_cents = item.shipping_cents
        comp.currency = item.currency
        comp.condition = result.condition or "unknown"
        comp.quantity = result.quantity
        comp.sold = item.sold
        comp.sold_at = item.sold_at
        comp.observed_at = now
        comp.match_score = result.score
        comp.excluded_reason = None if result.matched else result.reason

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

    # ── valuation ─────────────────────────────────────────────────────────

    async def revalue(self, variant_ids: Sequence[int] | None = None) -> dict[int, ValuationResult]:
        """Recompute valuations (all variants with comps when None). Emits `deal` events."""
        now = self._clock()
        results: dict[int, ValuationResult] = {}
        events = 0
        async with self._sessions() as session, session.begin():
            stmt = select(MarketComp).where(MarketComp.excluded_reason.is_(None))
            if variant_ids is not None:
                stmt = stmt.where(MarketComp.variant_id.in_(list(variant_ids)))
            by_variant: dict[int, list[Comp]] = {}
            for row in await session.scalars(stmt):
                by_variant.setdefault(row.variant_id, []).append(
                    Comp(
                        source=row.source,
                        price_cents=row.price_cents,
                        shipping_cents=row.shipping_cents,
                        quantity=row.quantity,
                        condition=row.condition,
                        sold=row.sold,
                        sold_at=row.sold_at,
                        observed_at=row.observed_at,
                        match_score=row.match_score,
                    )
                )
            for variant_id in variant_ids if variant_ids is not None else list(by_variant):
                comps = by_variant.get(variant_id, [])
                listing = await retail_listing(session, variant_id)
                available = bool(listing and StockStatus(listing.status).is_available)
                result = compute_valuation(
                    listing.price_cents if listing else None, comps, self._settings, now
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
        ):  # fmt: skip
            setattr(valuation, name, getattr(result, name))

        deal_now = is_deal(result, available, self._settings)
        fired = 0
        if deal_now and not valuation.deal_active and listing is not None:
            assert result.net_cents is not None
            assert result.roi is not None
            session.add(
                Event(
                    listing=listing,
                    kind=EventKind.DEAL.value,
                    occurred_at=now,
                    old_value=None,
                    new_value=f"net {format_cents(result.net_cents)} · ROI {result.roi:.0%}",
                )
            )
            fired = 1
        valuation.deal_active = deal_now
        return fired
