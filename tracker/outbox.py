"""Durable event outbox.

Ingestion writes `events` rows inside the same transaction as the state change. The
dispatcher later reads rows with `notified_at IS NULL`, hands them to every sink (log,
Discord, SMS, …), then stamps them. Alerts therefore survive restarts and sink outages.

Delivery is at-least-once: if one of several sinks fails, the batch stays pending and the
sinks that succeeded will see it again on the next dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Event, Listing, Product, Valuation, Variant
from tracker.domain import EventKind, StockStatus, format_cents

logger = logging.getLogger(__name__)

_HEADLINES = {
    EventKind.NEW_LISTING: "NEW",
    EventKind.BACK_IN_STOCK: "BACK IN STOCK",
    EventKind.SOLD_OUT: "SOLD OUT",
    EventKind.STATUS_CHANGE: "STATUS",
    EventKind.PRICE_CHANGE: "PRICE",
    EventKind.RESTOCK_ETA: "RESTOCK ETA",
    EventKind.EARLY_ACCESS: "EARLY ACCESS",
    EventKind.DELISTED: "DELISTED",
    EventKind.RELISTED: "RELISTED",
    EventKind.DEAL: "DEAL",
}


@dataclass(frozen=True, slots=True)
class RadarEvent:
    """An event joined with the listing context a human (or Discord embed) needs."""

    id: int
    kind: EventKind
    occurred_at: datetime
    old_value: str | None
    new_value: str | None
    listing_id: int | None
    source: str | None
    region: str | None
    url: str | None
    sku: str | None
    product_name: str | None
    category: str | None
    pack_size: int
    status: StockStatus
    price_cents: int | None
    currency: str | None
    restock_eta: date | None
    is_early_access: bool
    # Latest resale valuation of the variant, when one exists.
    resale_cents: int | None = None
    net_cents: int | None = None
    roi: float | None = None
    valuation_basis: str | None = None
    valuation_confidence: str | None = None
    comps_count: int = 0

    @property
    def profit_text(self) -> str | None:
        if self.net_cents is None or self.roi is None:
            return None
        sign = "+" if self.net_cents >= 0 else "−"
        return f"{sign}{format_cents(abs(self.net_cents))} est. ({self.roi:.0%} ROI)"

    def summary(self) -> str:
        parts = [_HEADLINES.get(self.kind, self.kind.upper())]
        if self.sku:
            parts.append(self.sku if not self.product_name else f"{self.sku} ({self.product_name})")
        if self.region:
            parts.append(self.region.upper())
        if self.kind in (EventKind.PRICE_CHANGE, EventKind.RESTOCK_ETA, EventKind.STATUS_CHANGE):
            parts.append(f"{self.old_value or '—'} → {self.new_value or '—'}")
        elif self.price_cents is not None:
            parts.append(format_cents(self.price_cents, self.currency))
        if self.pack_size > 1:
            parts.append(f"{self.pack_size}-pack")
        if self.is_early_access:
            parts.append("Early Access")
        if self.profit_text and self.kind is not EventKind.DEAL:
            parts.append(self.profit_text)
        if self.kind is EventKind.DEAL and self.new_value:
            parts.append(self.new_value)
        if self.restock_eta and self.kind is not EventKind.RESTOCK_ETA:
            parts.append(f"ETA {self.restock_eta.isoformat()}")
        if self.url:
            parts.append(self.url)
        return " · ".join(parts)


class EventSink(Protocol):
    name: str

    async def publish(self, events: Sequence[RadarEvent]) -> None: ...


class Dispatcher(Protocol):
    async def dispatch(self) -> int: ...


class LoggingSink:
    name = "log"

    def __init__(self, logger_name: str = "tracker.radar") -> None:
        self._logger = logging.getLogger(logger_name)

    async def publish(self, events: Sequence[RadarEvent]) -> None:
        for event in events:
            self._logger.info("%s", event.summary())


async def load_events(
    session: AsyncSession, *, pending_only: bool, limit: int, kinds: Sequence[str] | None = None
) -> list[RadarEvent]:
    stmt = (
        select(Event, Listing, Variant, Product, Valuation)
        .outerjoin(Listing, Event.listing_id == Listing.id)
        .outerjoin(Variant, Listing.variant_id == Variant.id)
        .outerjoin(Product, Variant.product_id == Product.id)
        .outerjoin(Valuation, Valuation.variant_id == Variant.id)
        .limit(limit)
    )
    if pending_only:
        stmt = stmt.where(Event.notified_at.is_(None)).order_by(Event.id)
    else:
        stmt = stmt.order_by(Event.id.desc())
    if kinds:
        stmt = stmt.where(Event.kind.in_(list(kinds)))

    events: list[RadarEvent] = []
    for event, listing, variant, product, valuation in (await session.execute(stmt)).all():
        # Only credible profit reaches alerts: real sales, your sales, or scarcity pricing.
        valued = valuation is not None and valuation.flip_net_cents is not None
        scarce = valued and valuation.flip_basis == "scarcity"
        events.append(
            RadarEvent(
                id=event.id,
                kind=EventKind(event.kind),
                occurred_at=event.occurred_at,
                old_value=event.old_value,
                new_value=event.new_value,
                listing_id=event.listing_id,
                source=listing.source if listing else None,
                region=listing.region if listing else None,
                url=listing.url if listing else None,
                sku=variant.sku if variant else None,
                product_name=product.name if product else None,
                category=product.category if product else None,
                pack_size=variant.pack_size if variant else 1,
                status=StockStatus(listing.status) if listing else StockStatus.UNKNOWN,
                price_cents=listing.price_cents if listing else None,
                currency=listing.currency if listing else None,
                restock_eta=listing.restock_eta if listing else None,
                is_early_access=bool(listing and listing.is_early_access),
                resale_cents=(
                    (valuation.scarcity_resale_cents if scarce else valuation.resale_cents)
                    if valued
                    else None
                ),
                net_cents=valuation.flip_net_cents if valued else None,
                roi=valuation.flip_roi if valued else None,
                valuation_basis=valuation.flip_basis if valued else None,
                valuation_confidence=valuation.flip_confidence if valued else None,
                comps_count=(
                    (valuation.scarcity_days if scarce else valuation.sold_count) if valued else 0
                ),
            )
        )
    return events


class EventDispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        sinks: Sequence[EventSink],
        *,
        max_age: timedelta = timedelta(hours=1),
        batch_size: int = 200,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._sinks = list(sinks)
        self._max_age = max_age
        self._batch_size = batch_size
        self._clock = clock
        self._lock = asyncio.Lock()

    async def dispatch(self) -> int:
        """Publish pending events. Returns how many were published (not counting stale ones)."""
        async with self._lock:
            published = 0
            while True:
                async with self._sessions() as session:
                    pending = await load_events(session, pending_only=True, limit=self._batch_size)
                if not pending:
                    return published

                now = self._clock()
                fresh = [e for e in pending if now - e.occurred_at <= self._max_age]
                stale = len(pending) - len(fresh)
                if stale:
                    logger.info("Recording %d stale event(s) without announcing them", stale)

                if fresh:
                    for sink in self._sinks:
                        try:
                            await sink.publish(fresh)
                        except Exception:
                            logger.exception("Sink %s failed; events stay pending", sink.name)
                            return published

                async with self._sessions() as session, session.begin():
                    await session.execute(
                        update(Event)
                        .where(Event.id.in_([e.id for e in pending]))
                        .values(notified_at=now)
                    )
                published += len(fresh)
                if len(pending) < self._batch_size:
                    return published
