"""Deal credibility: in-stock asking prices are capped; profit must come from real sales
or from scarcity pricing (eBay prices recorded while the store was sold out)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import make_snapshot
from tracker.db.models import EbayStatsHistory, Event, Listing, Valuation, Variant
from tracker.domain import EventKind, StockStatus
from tracker.history import scarcity_stats
from tracker.ingest import Ingestor
from tracker.market.service import MarketService
from tracker.market.valuation import (
    MarketStats,
    ScarcityStats,
    compute_valuation,
    in_stock_ceiling,
    is_deal,
)
from tracker.outbox import load_events
from tracker.settings import MarketSettings

SETTINGS = MarketSettings(_env_file=None)  # type: ignore[call-arg]
DAY0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
ACTIVE = MarketStats(active_count=9, active_p25_cents=22000, active_median_cents=23000)


def test_in_stock_asking_prices_are_capped():
    capped = compute_valuation(12900, ACTIVE, None, SETTINGS, available=True)
    assert capped.basis == "capped"
    assert capped.resale_cents == in_stock_ceiling(12900, SETTINGS) == 14448
    assert capped.net_cents < 0
    assert capped.flip_basis is None
    assert not is_deal(capped, True, SETTINGS)

    sold_out = compute_valuation(12900, ACTIVE, None, SETTINGS, available=False)
    assert sold_out.basis == "active"  # store sold out: asking prices are the market
    assert sold_out.resale_cents == 22000


def test_real_sales_are_never_capped():
    sold = MarketStats(sold_count=5, sold_30d=5, sold_median_cents=21000, active_count=9)
    result = compute_valuation(12900, sold, None, SETTINGS, available=True)
    assert result.basis == "sold"
    assert result.flip_basis == "sold"
    assert is_deal(result, True, SETTINGS)


@pytest.mark.parametrize(
    ("days", "sellers", "confidence", "deal"),
    [(1, 9, "none", False), (2, 9, "low", False), (4, 2, "low", False), (4, 6, "medium", True)],
)
def test_scarcity_flip_needs_enough_sold_out_days(days, sellers, confidence, deal):
    scarcity = ScarcityStats(days=days, resale_cents=22000, sellers=sellers)
    result = compute_valuation(12900, ACTIVE, None, SETTINGS, available=True, scarcity=scarcity)
    assert result.basis == "capped"  # today's in-stock market is still capped...
    assert result.flip_confidence == confidence
    if confidence != "none":
        assert result.flip_basis == "scarcity"  # ...but the sold-out market is the real flip
        assert result.scarcity_net_cents == result.flip_net_cents > 0
    assert is_deal(result, True, SETTINGS) is deal


# ── end to end: sold-out history → restock → credible deal ────────────────


@pytest.fixture
async def camera(sessions: async_sessionmaker[AsyncSession]):
    snap = make_snapshot(
        external_id="CAM-US", sku="CAM", product_key="ubiquiti:cam", price_cents=12900
    )
    async with sessions() as session, session.begin():
        ingestor = Ingestor(session)
        await ingestor.ingest([snap], observed_at=DAY0 - timedelta(days=1))  # in stock
        sold_out = make_snapshot(
            external_id="CAM-US",
            sku="CAM",
            product_key="ubiquiti:cam",
            price_cents=12900,
            status=StockStatus.OUT_OF_STOCK,
        )
        await ingestor.ingest([sold_out], observed_at=DAY0)  # sells out
        variant_id = await session.scalar(select(Variant.id).where(Variant.sku == "CAM"))
        # A snapshot from the in-stock day (doesn't count) and four sold-out days.
        days = [(-1, 15000, 9)] + [(d, 22000 + d * 100, 6) for d in range(4)]
        for offset, p25, sellers in days:
            session.add(
                EbayStatsHistory(
                    variant_id=variant_id,
                    day=(DAY0 + timedelta(days=offset)).date(),
                    fetched_at=DAY0 + timedelta(days=offset, hours=2),
                    active_count=sellers,
                    active_listings=sellers,
                    active_p25_cents=p25,
                )
            )
        # Today's eBay snapshot exists too (current asking market).
        from tracker.db.models import EbayStats

        session.add(
            EbayStats(
                variant_id=variant_id,
                fetched_at=DAY0 + timedelta(days=4),
                active_count=6,
                active_listings=6,
                active_p25_cents=21000,
                active_median_cents=22000,
            )
        )
    return sessions, variant_id


async def test_scarcity_stats_only_count_sold_out_days(camera):
    sessions, variant_id = camera
    async with sessions() as session:
        listing = await session.scalar(select(Listing))
        stats = await scarcity_stats(session, variant_id, listing.id, now=DAY0 + timedelta(days=4))
    assert stats.days == 4  # the in-stock day (p25 $150) is excluded
    assert stats.resale_cents == 22150
    assert stats.sellers == 6


class Dispatcher:
    async def dispatch(self) -> int:
        return 0


async def test_restock_fires_deal_with_scarcity_evidence(camera):
    sessions, variant_id = camera
    restock_at = DAY0 + timedelta(days=4, hours=6)
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                make_snapshot(
                    external_id="CAM-US", sku="CAM", product_key="ubiquiti:cam", price_cents=12900
                )
            ],
            observed_at=restock_at,
        )  # back in stock
    service = MarketService(sessions, None, SETTINGS, Dispatcher(), clock=lambda: restock_at)  # type: ignore[arg-type]
    result = (await service.revalue([variant_id]))[variant_id]

    assert result.basis == "capped"  # today's in-stock asking prices: not credible
    assert result.flip_basis == "scarcity"
    assert result.flip_confidence == "medium"
    async with sessions() as session:
        valuation = await session.get(Valuation, variant_id)
        assert valuation.deal_active
        deal = await session.scalar(select(Event).where(Event.kind == EventKind.DEAL.value))
        assert "sold-out resale ~$221.50 over 4 days" in deal.new_value
        events = await load_events(session, pending_only=True, limit=10)
    [deal_event] = [e for e in events if e.kind is EventKind.DEAL]
    assert deal_event.valuation_basis == "scarcity"
    assert deal_event.comps_count == 4
    assert deal_event.profit_text.startswith("+$")
    back = next(e for e in events if e.kind is EventKind.BACK_IN_STOCK)
    assert back.net_cents == deal_event.net_cents  # restock alert carries the credible profit


async def test_in_stock_item_without_evidence_shows_no_profit(sessions):
    async with sessions() as session, session.begin():
        ingestor = Ingestor(session)
        snap = {
            "external_id": "AP-US",
            "sku": "AP",
            "product_key": "ubiquiti:ap",
            "price_cents": 12900,
        }
        await ingestor.ingest(
            [make_snapshot(**snap, status=StockStatus.OUT_OF_STOCK)], observed_at=DAY0
        )
        await ingestor.ingest([make_snapshot(**snap)], observed_at=DAY0 + timedelta(hours=1))
        variant_id = await session.scalar(select(Variant.id))
        from tracker.db.models import EbayStats

        session.add(
            EbayStats(variant_id=variant_id, fetched_at=DAY0, active_count=9, active_listings=9,
                      active_p25_cents=22000, active_median_cents=23000)
        )  # fmt: skip
    service = MarketService(sessions, None, SETTINGS, Dispatcher(), clock=lambda: DAY0)  # type: ignore[arg-type]
    result = (await service.revalue([variant_id]))[variant_id]
    assert result.basis == "capped"
    assert result.flip_basis is None
    async with sessions() as session:
        events = await load_events(session, pending_only=True, limit=10)
        assert all(e.kind is not EventKind.DEAL for e in events)
        back = next(e for e in events if e.kind is EventKind.BACK_IN_STOCK)
        assert back.profit_text is None  # no fake "+$78 est." on the alert
