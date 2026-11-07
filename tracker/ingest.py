"""Turn source snapshots into stored state, history, and radar events.

Rules:
* A listing's state is only written to history when something actually changed.
* An `unknown` status never overwrites a known one (scrape failures must not erase state
  and cause missed back-in-stock transitions).
* `new_listing` events only fire for a (source, region) whose first *complete* crawl has
  been recorded (`mark_baseline_complete`). Until then everything is baseline: rows are
  created silently. Sources that are never crawled (URL retailers) never fire it.
* Delisting is inferred, never observed: `mark_delisted()` flags listings that no source has
  confirmed for a grace period. Seeing a delisted listing again relists it.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import Event, Listing, Meta, Product, StockObservation, Variant
from tracker.domain import EventKind, ListingSnapshot, StockStatus, format_cents

logger = logging.getLogger(__name__)


def baseline_key(source: str, region: str) -> str:
    return f"baseline:{source}:{region}"


@dataclass(slots=True)
class IngestReport:
    created_listings: int = 0
    observations: int = 0
    unchanged: int = 0
    unknown_skipped: int = 0
    events: list[Event] = field(default_factory=list)

    @property
    def event_counts(self) -> Counter[str]:
        return Counter(event.kind for event in self.events)


class Ingestor:
    def __init__(self, session: AsyncSession, *, emit_events: bool = True) -> None:
        self._session = session
        self._emit_events = emit_events
        self._baseline: dict[tuple[str, str], bool] = {}
        self._products: dict[str, Product] = {}
        self._variants: dict[str, Variant] = {}

    async def ingest(
        self, snapshots: Iterable[ListingSnapshot], observed_at: datetime | None = None
    ) -> IngestReport:
        now = observed_at or datetime.now(UTC)
        report = IngestReport()
        for snapshot in snapshots:
            await self._ingest_one(snapshot, now, report)
        await self._session.flush()
        return report

    async def mark_delisted(
        self,
        source: str,
        region: str,
        *,
        stale_before: datetime,
        now: datetime | None = None,
        max_count: int | None = None,
    ) -> list[Event]:
        """Flag listings in (source, region) not confirmed since `stale_before` as delisted.

        Call only after a *complete* crawl of that source/region, otherwise a failed
        category would delist everything in it. More than `max_count` candidates looks like
        a store-side identity change (e.g. renamed SKUs), so nothing is delisted.
        """
        now = now or datetime.now(UTC)
        stale = (
            await self._session.scalars(
                select(Listing).where(
                    Listing.source == source,
                    Listing.region == region,
                    Listing.enabled.is_(True),
                    Listing.delisted_at.is_(None),
                    Listing.last_checked_at < stale_before,
                )
            )
        ).all()
        if max_count is not None and len(stale) > max_count:
            logger.error(
                "%s/%s: %d listings look delisted at once (limit %d); not delisting. "
                "Did the store change its SKUs?",
                source,
                region,
                len(stale),
                max_count,
            )
            return []
        events: list[Event] = []
        for listing in stale:
            listing.delisted_at = now
            listing.last_changed_at = now
            if self._emit_events:
                event = Event(
                    listing=listing,
                    kind=EventKind.DELISTED.value,
                    occurred_at=now,
                    old_value=listing.status,
                    new_value=None,
                )
                self._session.add(event)
                events.append(event)
        await self._session.flush()
        return events

    async def is_baseline(self, source: str, region: str) -> bool:
        key = (source, region)
        if key not in self._baseline:
            marker = await self._session.get(Meta, baseline_key(source, region))
            self._baseline[key] = marker is None
        return self._baseline[key]

    async def mark_baseline_complete(self, source: str, region: str, now: datetime) -> None:
        if await self._session.get(Meta, baseline_key(source, region)) is None:
            self._session.add(Meta(key=baseline_key(source, region), value=now.isoformat()))
        self._baseline[(source, region)] = False

    @staticmethod
    def _refresh_product(product: Product, snapshot: ListingSnapshot) -> None:
        product.name = snapshot.product_name
        if snapshot.category:
            product.category = snapshot.category

    async def _product(self, snapshot: ListingSnapshot) -> Product:
        product = self._products.get(snapshot.product_key)
        if product is None:
            product = await self._session.scalar(
                select(Product).where(Product.key == snapshot.product_key)
            )
        if product is None:
            product = Product(key=snapshot.product_key, brand=snapshot.brand)
            self._session.add(product)
        self._refresh_product(product, snapshot)
        self._products[snapshot.product_key] = product
        return product

    async def _variant(self, snapshot: ListingSnapshot) -> Variant:
        """Variants are shared across retailers by SKU. Only the source that owns the
        product identity (matching product_key) may rename or recategorize the product."""
        sku_key = snapshot.sku.lower()
        variant = self._variants.get(sku_key)
        if variant is None:
            variant = await self._session.scalar(select(Variant).where(Variant.sku_key == sku_key))
        if variant is None:
            variant = Variant(
                product=await self._product(snapshot), sku=snapshot.sku, sku_key=sku_key
            )
            self._session.add(variant)
        elif variant.product_id is not None:
            product = await self._session.get(Product, variant.product_id)
            if product is not None and product.key == snapshot.product_key:
                self._refresh_product(product, snapshot)
        self._variants[sku_key] = variant
        if snapshot.variant_title is not None:
            variant.title = snapshot.variant_title
        variant.pack_size = snapshot.pack_size
        return variant

    async def _ingest_one(
        self, snapshot: ListingSnapshot, now: datetime, report: IngestReport
    ) -> None:
        baseline = await self.is_baseline(snapshot.source, snapshot.region)
        listing = await self._session.scalar(
            select(Listing).where(
                Listing.source == snapshot.source.value,
                Listing.region == snapshot.region,
                Listing.external_id == snapshot.external_id,
            )
        )
        events: list[tuple[EventKind, object, object]] = []
        created = listing is None

        if listing is None:
            listing = Listing(
                variant=await self._variant(snapshot),
                source=snapshot.source.value,
                region=snapshot.region,
                external_id=snapshot.external_id,
                url=snapshot.url,
                status=StockStatus.UNKNOWN.value,
                is_early_access=False,
                consecutive_unknowns=0,
            )
            self._session.add(listing)
            report.created_listings += 1
            if not baseline:
                events.append((EventKind.NEW_LISTING, None, snapshot.sku))
        else:
            await self._variant(snapshot)
            listing.url = snapshot.url

        listing.last_checked_at = now
        relisted = listing.delisted_at is not None
        if relisted:
            listing.delisted_at = None
            events.append((EventKind.RELISTED, None, snapshot.sku))

        if not snapshot.status.is_known:
            listing.consecutive_unknowns += 1
            report.unknown_skipped += 1
            if not created and not relisted:
                return
        else:
            listing.consecutive_unknowns = 0

        changed = self._apply(listing, snapshot, now, events) or created or relisted
        if not changed:
            report.unchanged += 1
            return

        listing.last_changed_at = now
        self._session.add(
            StockObservation(
                listing=listing,
                observed_at=now,
                status=listing.status,
                price_cents=listing.price_cents,
                regular_price_cents=listing.regular_price_cents,
                quantity=snapshot.quantity,
                is_early_access=listing.is_early_access,
                restock_eta=listing.restock_eta,
            )
        )
        report.observations += 1

        if self._emit_events:
            for kind, old, new in events:
                event = Event(
                    listing=listing,
                    kind=kind.value,
                    occurred_at=now,
                    old_value=None if old is None else str(old),
                    new_value=None if new is None else str(new),
                )
                self._session.add(event)
                report.events.append(event)

    def _apply(
        self,
        listing: Listing,
        snapshot: ListingSnapshot,
        now: datetime,
        events: list[tuple[EventKind, object, object]],
    ) -> bool:
        """Copy snapshot state onto the listing. Returns True if anything changed.

        For a brand-new listing (status still unknown) the comparisons produce no events
        except `new_listing`, which the caller already queued.
        """
        old_status = StockStatus(listing.status)
        is_new = not old_status.is_known and listing.last_changed_at is None
        changed = False

        if snapshot.status.is_known and snapshot.status != old_status:
            if old_status.is_known:
                if snapshot.status.is_available and not old_status.is_available:
                    events.append((EventKind.BACK_IN_STOCK, old_status, snapshot.status))
                elif old_status.is_available and not snapshot.status.is_available:
                    events.append((EventKind.SOLD_OUT, old_status, snapshot.status))
                else:
                    events.append((EventKind.STATUS_CHANGE, old_status, snapshot.status))
            listing.status = snapshot.status.value
            changed = True
        listing.raw_status = snapshot.raw_status or listing.raw_status

        if snapshot.status.is_available:
            listing.last_in_stock_at = now

        if snapshot.price_cents is not None and snapshot.price_cents != listing.price_cents:
            if listing.price_cents is not None and not is_new:
                events.append(
                    (
                        EventKind.PRICE_CHANGE,
                        format_cents(listing.price_cents, listing.currency),
                        format_cents(snapshot.price_cents, snapshot.currency),
                    )
                )
            listing.price_cents = snapshot.price_cents
            changed = True
        if snapshot.currency:
            listing.currency = snapshot.currency
        if (
            snapshot.price_cents is not None
            and snapshot.regular_price_cents != listing.regular_price_cents
        ):
            listing.regular_price_cents = snapshot.regular_price_cents
            changed = True

        if snapshot.restock_eta != listing.restock_eta:
            if snapshot.restock_eta is not None and not is_new:
                events.append((EventKind.RESTOCK_ETA, listing.restock_eta, snapshot.restock_eta))
            listing.restock_eta = snapshot.restock_eta
            changed = True

        if snapshot.sold_out_at is not None:
            listing.sold_out_at = snapshot.sold_out_at

        if (
            snapshot.is_early_access is not None
            and snapshot.is_early_access != listing.is_early_access
        ):
            if snapshot.is_early_access and not is_new:
                events.append((EventKind.EARLY_ACCESS, None, snapshot.sku))
            listing.is_early_access = snapshot.is_early_access
            changed = True

        return changed
