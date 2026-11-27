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
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Event, EventDelivery, Listing, Product, Valuation, Variant
from tracker.domain import EventKind, StockStatus, format_cents, region_tag

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
        if tag := region_tag(self.region):
            parts.append(tag)
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
    session: AsyncSession,
    *,
    pending_only: bool,
    limit: int,
    kinds: Sequence[str] | None = None,
    regions: Collection[str] | None = None,
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
    if regions is not None:
        stmt = stmt.where(or_(Listing.region.in_(list(regions)), Event.listing_id.is_(None)))

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
        # For the watchdog: alerts given up on, and destinations that are failing right now.
        self.expired = 0
        self.failing: dict[str, str] = {}  # sink name -> last error

    def add_sink(self, sink: EventSink) -> None:
        """Register a destination after construction (Discord, SMS, ntfy are built later)."""
        self._sinks.append(sink)

    @property
    def sink_names(self) -> list[str]:
        return [sink.name for sink in self._sinks]

    async def dispatch(self) -> int:
        """Deliver pending events to every destination. Returns how many were fully delivered.

        Each destination (sink) is tried on its own, all at once: one that's down (Discord
        reconnecting) doesn't hold back the others (the phone push), and one that already
        delivered an event isn't asked again while another is retried. An event is done when
        every destination has it, or when it's older than ``max_age``; then it's given up on and
        counted in ``expired`` (the watchdog tells the owner).
        """
        async with self._lock:
            done = 0
            while True:
                async with self._sessions() as session:
                    pending = await load_events(session, pending_only=True, limit=self._batch_size)
                    delivered = await _delivered(session, [e.id for e in pending])
                if not pending:
                    return done

                now = self._clock()
                fresh = [e for e in pending if now - e.occurred_at <= self._max_age]
                stale = [e for e in pending if now - e.occurred_at > self._max_age]
                results = await asyncio.gather(
                    *(
                        self._publish(
                            sink, [e for e in fresh if sink.name not in delivered.get(e.id, ())]
                        )
                        for sink in self._sinks
                    )
                )
                for sink, ids in zip(self._sinks, results, strict=True):
                    for event_id in ids:
                        delivered.setdefault(event_id, set()).add(sink.name)
                complete = [
                    e.id
                    for e in fresh
                    if all(s.name in delivered.get(e.id, ()) for s in self._sinks)
                ]
                missed = [
                    e
                    for e in stale
                    if any(s.name not in delivered.get(e.id, ()) for s in self._sinks)
                ]
                if missed:
                    self.expired += len(missed)
                    logger.warning(
                        "Gave up on %d alert(s) older than %s that never reached every destination",
                        len(missed),
                        self._max_age,
                    )
                finished = complete + [e.id for e in stale]
                async with self._sessions() as session, session.begin():
                    for sink, ids in zip(self._sinks, results, strict=True):
                        session.add_all(
                            EventDelivery(event_id=i, sink=sink.name, delivered_at=now) for i in ids
                        )
                    if finished:
                        await session.execute(
                            update(Event).where(Event.id.in_(finished)).values(notified_at=now)
                        )
                done += len(complete)
                if len(pending) < self._batch_size or not finished:
                    return done

    async def _publish(self, sink: EventSink, events: list[RadarEvent]) -> list[int]:
        """The ids this destination delivered (all of them, or none when it failed)."""
        if not events:
            return []
        try:
            await sink.publish(events)
        except Exception as exc:
            if sink.name not in self.failing:
                logger.exception("Alert destination %s failed; its alerts stay pending", sink.name)
            self.failing[sink.name] = f"{type(exc).__name__}: {exc}"[:200]
            return []
        if self.failing.pop(sink.name, None) is not None:
            logger.info("Alert destination %s is delivering again", sink.name)
        return [e.id for e in events]


async def _delivered(session: AsyncSession, event_ids: list[int]) -> dict[int, set[str]]:
    if not event_ids:
        return {}
    rows = await session.execute(
        select(EventDelivery.event_id, EventDelivery.sink).where(
            EventDelivery.event_id.in_(event_ids)
        )
    )
    found: dict[int, set[str]] = {}
    for event_id, sink in rows.all():
        found.setdefault(event_id, set()).add(sink)
    return found
