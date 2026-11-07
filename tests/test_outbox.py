from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.db.models import Event
from tracker.domain import EventKind, StockStatus
from tracker.ingest import Ingestor
from tracker.outbox import EventDispatcher, RadarEvent, load_events


class RecordingSink:
    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[RadarEvent]] = []
        self.fail = fail

    async def publish(self, events: Sequence[RadarEvent]) -> None:
        if self.fail:
            raise RuntimeError("sink down")
        self.batches.append(list(events))


async def _sold_out_event(sessions: async_sessionmaker[AsyncSession], when=T0) -> None:
    async with sessions() as session, session.begin():
        ingestor = Ingestor(session)
        await ingestor.ingest([make_snapshot()], observed_at=when - timedelta(minutes=5))
        await ingestor.ingest(
            [make_snapshot(status=StockStatus.OUT_OF_STOCK, restock_eta=None)], observed_at=when
        )


async def test_dispatch_publishes_once_with_context(sessions):
    await _sold_out_event(sessions)
    sink = RecordingSink()
    dispatcher = EventDispatcher(sessions, [sink], clock=lambda: T0 + timedelta(minutes=1))

    assert await dispatcher.dispatch() == 1
    assert await dispatcher.dispatch() == 0

    [event] = sink.batches[0]
    assert event.kind is EventKind.SOLD_OUT
    assert event.sku == "U7-Pro"
    assert event.product_name == "Access Point U7 Pro"
    assert event.status is StockStatus.OUT_OF_STOCK
    assert event.url == "https://store.ui.com/us/en/products/u7-pro"
    summary = event.summary()
    assert summary.startswith("SOLD OUT · U7-Pro (Access Point U7 Pro) · US · $189.00")


async def test_failed_sink_leaves_events_pending(sessions):
    await _sold_out_event(sessions)
    clock = lambda: T0 + timedelta(minutes=1)  # noqa: E731
    assert await EventDispatcher(sessions, [RecordingSink(fail=True)], clock=clock).dispatch() == 0

    healthy = RecordingSink()
    assert await EventDispatcher(sessions, [healthy], clock=clock).dispatch() == 1
    assert len(healthy.batches) == 1


async def test_stale_events_are_recorded_not_announced(sessions):
    await _sold_out_event(sessions)
    sink = RecordingSink()
    dispatcher = EventDispatcher(
        sessions, [sink], max_age=timedelta(minutes=30), clock=lambda: T0 + timedelta(hours=2)
    )
    assert await dispatcher.dispatch() == 0
    assert sink.batches == []
    async with sessions() as session:
        event = await session.scalar(select(Event))
        assert event.notified_at is not None


async def test_load_events_newest_first_with_kind_filter(sessions):
    await _sold_out_event(sessions)
    async with sessions() as session:
        assert len(await load_events(session, pending_only=False, limit=10)) == 1
        assert await load_events(session, pending_only=False, limit=10, kinds=["delisted"]) == []
