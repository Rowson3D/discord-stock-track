"""G1: daily eBay aggregate history, refresh tiers, stock-cycle metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.bot.commands import history_lines, trend_text
from tracker.db.models import EbayStatsHistory, Listing, Variant
from tracker.domain import StockStatus
from tracker.history import (
    StockPoint,
    ebay_trend,
    humanize,
    stock_history,
    summarize_stock,
)
from tracker.ingest import Ingestor
from tracker.market.ebay import EbayItem
from tracker.market.service import MarketService
from tracker.settings import MarketSettings

IN, OUT = StockStatus.IN_STOCK, StockStatus.OUT_OF_STOCK
SETTINGS = MarketSettings(_env_file=None)  # type: ignore[call-arg]


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ── stock cycles ──────────────────────────────────────────────────────────


def test_restock_windows_measure_sellout_speed():
    history = summarize_stock(
        [
            StockPoint(at(0), IN),  # baseline: window start unknown, not counted
            StockPoint(at(100), OUT),
            StockPoint(at(1000), IN),  # restock
            StockPoint(at(1040), OUT),  # sold out in 40m
            StockPoint(at(5000), IN),  # restock
            StockPoint(at(5020), OUT),  # sold out in 20m
        ]
    )
    assert history.restocks == 2
    assert history.sellout_times == [timedelta(minutes=40), timedelta(minutes=20)]
    assert history.median_sellout == timedelta(minutes=30)
    assert history.unavailable_since == at(5020)
    assert not history.unavailable_at_least
    assert history.out_of_stock_for(at(5020 + 60 * 24 * 3)) == timedelta(days=3)


def test_sold_out_since_before_tracking_is_flagged():
    history = summarize_stock([StockPoint(at(0), OUT)])
    assert history.unavailable_at_least
    assert history.restocks == 0
    assert history.median_sellout is None


def test_currently_in_stock_has_no_outage():
    history = summarize_stock(
        [StockPoint(at(0), OUT), StockPoint(at(10), IN), StockPoint(at(20), StockStatus.UNKNOWN)]
    )
    assert history.available_since == at(10)
    assert history.out_of_stock_for(at(99)) is None


def test_humanize():
    assert humanize(timedelta(seconds=30)) == "1m"
    assert humanize(timedelta(minutes=42)) == "42m"
    assert humanize(timedelta(hours=5, minutes=3)) == "5h 3m"
    assert humanize(timedelta(days=23, hours=4)) == "23d"
    assert humanize(None) == "—"


# ── catalog fixture ───────────────────────────────────────────────────────


@pytest.fixture
async def store(sessions: async_sessionmaker[AsyncSession]):
    def snap(sku: str, cents: int, **kw: object):
        return make_snapshot(
            external_id=f"{sku}-US",
            sku=sku,
            url=f"https://store.ui.com/us/en/products/{sku.lower()}",
            product_key=f"ubiquiti:{sku.lower()}",
            product_name=sku,
            price_cents=cents,
            **kw,
        )

    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                snap("NORMAL-AP", 20000),
                snap("SOLD-OUT-AP", 30000, status=OUT),
                snap("GONE-AP", 40000),
                snap("HOT-AP", 25000, status=StockStatus.COMING_SOON),
            ],
            observed_at=T0,
        )
        gone = await session.scalar(select(Listing).where(Listing.external_id == "GONE-AP-US"))
        gone.delisted_at = T0
    return sessions


async def _vid(sessions, sku: str) -> int:
    async with sessions() as session:
        return await session.scalar(select(Variant.id).where(Variant.sku == sku))


class FakeEbay:
    def __init__(self, prices: list[int]) -> None:
        self.prices = prices

    async def search_active(self, query, *, min_price_cents=None, limit=100):
        sku = query.removeprefix("ubiquiti ").replace(" ", "-")
        return [
            EbayItem(
                f"{sku}{i}", f"Ubiquiti {sku}", None, p, 0, "USD", "1000", False, seller=f"s{i}"
            )
            for i, p in enumerate(self.prices)
        ]

    async def search_sold(self, query, *, min_price_cents=None, limit=100):
        return []


class Dispatcher:
    async def dispatch(self) -> int:
        return 0


def service(sessions, now: datetime, prices=(50000, 51000, 52000)) -> MarketService:
    return MarketService(
        sessions, FakeEbay(list(prices)), SETTINGS, Dispatcher(), clock=lambda: now
    )  # type: ignore[arg-type]


# ── refresh tiers ─────────────────────────────────────────────────────────


async def test_refresh_tiers_include_delisted_items(store):
    ids = {sku: await _vid(store, sku) for sku in ("NORMAL-AP", "SOLD-OUT-AP", "GONE-AP", "HOT-AP")}
    svc = service(store, T0)
    # Never checked: hot first, then end-of-life candidates (incl. delisted), then normal.
    assert await svc.due_variants(10) == [
        ids["HOT-AP"], ids["GONE-AP"], ids["SOLD-OUT-AP"], ids["NORMAL-AP"]
    ]  # fmt: skip

    async with store() as session, session.begin():
        for vid in ids.values():
            (await session.get(Variant, vid)).comps_checked_at = T0
    for hours, expected in (
        (7, {"HOT-AP"}),  # hot refreshes after 6h
        (25, {"HOT-AP", "GONE-AP", "SOLD-OUT-AP"}),  # end-of-life candidates daily
        (73, {"HOT-AP", "GONE-AP", "SOLD-OUT-AP", "NORMAL-AP"}),  # everything else 72h
    ):
        due = await service(store, T0 + timedelta(hours=hours)).due_variants(10)
        assert {sku for sku, vid in ids.items() if vid in due} == expected


# ── daily aggregate history ───────────────────────────────────────────────


async def test_collect_writes_one_history_row_per_day(store):
    vid = await _vid(store, "SOLD-OUT-AP")
    day1 = datetime(2026, 11, 7, 9, tzinfo=UTC)
    await service(store, day1, (50000, 52000, 54000)).collect(vid)
    await service(store, day1 + timedelta(hours=8), (51000, 53000, 55000)).collect(vid)
    await service(store, day1 + timedelta(days=7), (60000, 62000)).collect(vid)

    async with store() as session:
        rows = list(
            await session.scalars(
                select(EbayStatsHistory)
                .where(EbayStatsHistory.variant_id == vid)
                .order_by(EbayStatsHistory.day)
            )
        )
        assert [r.day.isoformat() for r in rows] == ["2026-11-07", "2026-11-14"]
        assert rows[0].fetched_at == day1 + timedelta(hours=8)  # latest of the day wins
        assert rows[0].active_count == 3
        assert rows[0].retail_cents == 30000
        trend = await ebay_trend(session, vid)

    assert trend.days == 7
    assert (trend.sellers_then, trend.sellers_now) == (3, 2)
    assert round(trend.sellers_change, 3) == -0.333
    assert trend.price_change > 0
    text = trend_text(trend)
    assert "Sellers 3 → 2 (-33%)" in text
    assert "2 daily snapshots" in text


async def test_trend_needs_two_snapshots(store):
    vid = await _vid(store, "NORMAL-AP")
    await service(store, T0).collect(vid)
    async with store() as session:
        assert await ebay_trend(session, vid) is None
        assert await session.scalar(select(func.count()).select_from(EbayStatsHistory)) == 1


# ── surfaced lines ────────────────────────────────────────────────────────


async def test_history_lines_for_stock_command(store):
    async with store() as session, session.begin():
        listing = await session.scalar(
            select(Listing).where(Listing.external_id == "SOLD-OUT-AP-US")
        )
        listing.raw_status = "RegionNotAvailable"
    async with store() as session:
        listing = await session.scalar(
            select(Listing).where(Listing.external_id == "SOLD-OUT-AP-US")
        )
        history = await stock_history(session, listing.id)
        lines = history_lines(listing, history, T0 + timedelta(days=12))
        assert lines == ["🌍 Withdrawn from this region", "⌛ Out of stock 12d+"]

        gone = await session.scalar(select(Listing).where(Listing.external_id == "GONE-AP-US"))
        assert history_lines(gone, await stock_history(session, gone.id), T0)[0].startswith(
            "🗑️ Delisted"
        )


async def test_cheap_packs_and_accessories_dont_spend_ebay_quota(sessions):
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                make_snapshot(
                    external_id="CABLE-10-US", sku="CABLE-10", product_key="ubiquiti:cable",
                    price_cents=5000, pack_size=10, variant_title="10-Pack",
                ),  # $50 pack, $5/unit
                make_snapshot(
                    external_id="MOUNT-US", sku="UACC-MOUNT", product_key="ubiquiti:mount",
                    price_cents=4900, category="accessories-mounts",
                ),
                make_snapshot(external_id="AP-US", sku="REAL-AP", product_key="ubiquiti:ap"),
            ],
            observed_at=T0,
        )  # fmt: skip
    due = await service(sessions, T0).due_variants(10)
    assert due == [await _vid(sessions, "REAL-AP")]
