from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import T0, make_snapshot
from tracker.db.models import Event, Listing, Product, StockObservation, Variant
from tracker.domain import EventKind, SourceKey, StockStatus
from tracker.ingest import Ingestor


async def _count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def _listing(session: AsyncSession, external_id: str = "U7-Pro-US") -> Listing:
    return await session.scalar(select(Listing).where(Listing.external_id == external_id))


async def test_baseline_ingest_creates_rows_without_events(session: AsyncSession):
    report = await Ingestor(session).ingest(
        [make_snapshot(), make_snapshot(external_id="U7-Pro-5-US", sku="U7-Pro-5", pack_size=5)],
        observed_at=T0,
    )

    assert report.created_listings == 2
    assert report.observations == 2
    assert report.events == []
    assert await _count(session, Product) == 1
    assert await _count(session, Variant) == 2
    listing = await _listing(session)
    assert listing.status == StockStatus.IN_STOCK
    assert listing.price_cents == 18900
    assert listing.last_checked_at == T0
    assert listing.last_in_stock_at == T0


async def test_unchanged_snapshot_writes_no_history(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot()], observed_at=T0)
    report = await ingestor.ingest([make_snapshot()], observed_at=T0 + timedelta(minutes=1))

    assert report.unchanged == 1
    assert report.observations == 0
    assert await _count(session, StockObservation) == 1
    assert (await _listing(session)).last_checked_at == T0 + timedelta(minutes=1)


async def test_sold_out_then_back_in_stock_events(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot()], observed_at=T0)

    sold_out = await ingestor.ingest(
        [make_snapshot(status=StockStatus.OUT_OF_STOCK, raw_status="SoldOut")],
        observed_at=T0 + timedelta(hours=1),
    )
    assert [(e.kind, e.old_value, e.new_value) for e in sold_out.events] == [
        (EventKind.SOLD_OUT, "in_stock", "out_of_stock")
    ]

    back = await ingestor.ingest([make_snapshot()], observed_at=T0 + timedelta(hours=2))
    assert [e.kind for e in back.events] == [EventKind.BACK_IN_STOCK]
    assert await _count(session, StockObservation) == 3
    assert await _count(session, Event) == 2


async def test_coming_soon_to_available_is_back_in_stock(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot(status=StockStatus.COMING_SOON)], observed_at=T0)
    report = await ingestor.ingest([make_snapshot()], observed_at=T0 + timedelta(hours=1))
    assert [e.kind for e in report.events] == [EventKind.BACK_IN_STOCK]


async def test_unknown_status_never_overwrites_known_state(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot(status=StockStatus.OUT_OF_STOCK)], observed_at=T0)

    report = await ingestor.ingest(
        [make_snapshot(status=StockStatus.UNKNOWN, price_cents=1)], observed_at=T0 + timedelta(1)
    )
    listing = await _listing(session)
    assert report.unknown_skipped == 1
    assert report.events == []
    assert listing.status == StockStatus.OUT_OF_STOCK
    assert listing.price_cents == 18900
    assert listing.consecutive_unknowns == 1

    # The next real result still produces the transition the unknown would have hidden.
    back = await ingestor.ingest([make_snapshot()], observed_at=T0 + timedelta(2))
    assert [e.kind for e in back.events] == [EventKind.BACK_IN_STOCK]
    assert (await _listing(session)).consecutive_unknowns == 0


async def test_price_restock_and_early_access_events(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot()], observed_at=T0)

    report = await ingestor.ingest(
        [
            make_snapshot(
                price_cents=16900,
                regular_price_cents=18900,
                restock_eta=date(2026, 12, 1),
                is_early_access=True,
            )
        ],
        observed_at=T0 + timedelta(hours=1),
    )
    events = {e.kind: (e.old_value, e.new_value) for e in report.events}
    assert events[EventKind.PRICE_CHANGE] == ("$189.00", "$169.00")
    assert events[EventKind.RESTOCK_ETA] == (None, "2026-12-01")
    assert events[EventKind.EARLY_ACCESS] == (None, "U7-Pro")
    assert report.observations == 1


async def test_category_view_without_early_access_flag_keeps_it(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot(is_early_access=True)], observed_at=T0)
    report = await ingestor.ingest(
        [make_snapshot(is_early_access=None)], observed_at=T0 + timedelta(1)
    )
    assert report.unchanged == 1
    assert (await _listing(session)).is_early_access is True


async def test_new_listing_after_baseline_emits_event(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot()], observed_at=T0)

    report = await Ingestor(session).ingest(
        [make_snapshot(external_id="U7-Pro-Max-US", sku="U7-Pro-Max", product_key="ubiquiti:max")],
        observed_at=T0 + timedelta(hours=1),
    )
    assert [(e.kind, e.new_value) for e in report.events] == [(EventKind.NEW_LISTING, "U7-Pro-Max")]


async def test_emit_events_false_records_history_silently(session: AsyncSession):
    await Ingestor(session).ingest([make_snapshot()], observed_at=T0)
    report = await Ingestor(session, emit_events=False).ingest(
        [make_snapshot(status=StockStatus.OUT_OF_STOCK)], observed_at=T0 + timedelta(1)
    )
    assert report.observations == 1
    assert report.events == []
    assert await _count(session, Event) == 0


async def test_other_retailer_shares_variant_without_hijacking_product(session: AsyncSession):
    ingestor = Ingestor(session)
    await ingestor.ingest([make_snapshot()], observed_at=T0)
    await ingestor.ingest(
        [
            make_snapshot(
                source=SourceKey.AMAZON,
                external_id="https://www.amazon.com/dp/B0EXAMPLE1",
                product_key="amazon:u7-pro",
                product_name="Ubiquiti U7 Pro WiFi 7 Access Point (Amazon title)",
                brand="amazon",
            )
        ],
        observed_at=T0,
    )

    assert await _count(session, Variant) == 1
    assert await _count(session, Listing) == 2
    products = (await session.scalars(select(Product))).all()
    assert [p.name for p in products] == ["Access Point U7 Pro"]
