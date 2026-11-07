"""Seller de-duplication and the read-only /scan."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.db.models import EbayStats, Event, MarketComp, Valuation
from tracker.domain import StockStatus
from tracker.guilds import GuildConfig, GuildStatus
from tracker.ingest import Ingestor
from tracker.market.ebay import EbayItem, parse_active
from tracker.market.scan import breakeven_unit_cents, run_scan
from tracker.market.service import MarketService
from tracker.market.valuation import Comp, dedupe_sellers, summarize
from tracker.permissions import Role, check_access
from tracker.settings import MarketSettings

SETTINGS = MarketSettings(_env_file=None)  # type: ignore[call-arg]


def active(price: int, seller: str | None) -> Comp:
    return Comp("ebay_active", price, 0, 1, "new", False, None, T0, 1.0, seller)


def test_one_seller_counts_once_at_their_cheapest():
    comps = [active(p, "dropship") for p in (24000, 23000, 22900, 23500)] + [
        active(20000, "alice"),
        active(21000, "bob"),
        active(19000, None),  # unknown sellers count individually
        active(19500, None),
    ]
    kept = dedupe_sellers(comps)
    assert len(kept) == 5
    assert min(c.unit_cents for c in kept if c.seller == "dropship") == 22900
    stats = summarize(comps, 12900, SETTINGS, T0)
    assert stats.active_listings == 8
    assert stats.active_count == 5


def test_sellers_parsed_from_browse_payload():
    payload = {
        "itemSummaries": [
            {"itemId": "1", "title": "x", "price": {"value": "10"}, "seller": {"username": "Bob"}},
            {"itemId": "2", "title": "y", "price": {"value": "11"}},
        ]
    }
    items = parse_active(payload)
    assert [i.seller for i in items] == ["bob", None]


def test_breakeven_formula():
    # x - 0.136x - $0.40 - $12 = $119.67  →  x ≈ $152.86
    assert breakeven_unit_cents(11967, SETTINGS) == 15286


class FakeEbay:
    def __init__(self, listings: dict[str, list[EbayItem]]) -> None:
        self.listings = listings
        self.queries: list[str] = []

    async def search_active(self, query: str, *, min_price_cents=None, limit=100):
        self.queries.append(query)
        return self.listings.get(query, [])

    async def search_sold(self, query: str, *, min_price_cents=None, limit=100):
        return []


def item(n: int, title: str, cents: int, seller: str) -> EbayItem:
    return EbayItem(f"i{n}", title, None, cents, 0, "USD", "1000", False, seller=seller)


@pytest.fixture
async def shop(sessions: async_sessionmaker[AsyncSession]):
    def snap(sku: str, slug: str, cents: int, pack: int = 1, **kw: object):
        return make_snapshot(
            external_id=f"{sku}-US", sku=sku, url=f"https://store.ui.com/us/en/products/{slug}",
            product_key=f"ubiquiti:{slug}", product_name="Camera G5 Bullet", price_cents=cents,
            pack_size=pack, category="physical-security-bullet", **kw,
        )  # fmt: skip

    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                snap("UVC-G5-Bullet", "uvc-g5-bullet", 12900),
                snap("UVC-G5-Bullet-3", "uvc-g5-bullet", 35900, pack=3, variant_title="3-Pack"),
                make_snapshot(
                    external_id="UACC-Cable-US",
                    sku="UACC-Cable",
                    price_cents=500,
                    category="accessories-cables",
                ),
            ],
            observed_at=T0,
        )
    return sessions


async def test_scan_is_read_only_and_finds_breakouts(shop):
    titles = "Ubiquiti UniFi Protect G5 Bullet Camera UVC-G5-Bullet"
    ebay = FakeEbay(
        {
            "ubiquiti UVC G5 Bullet": [
                item(1, titles, 22000, "a"),
                item(2, titles, 22500, "b"),
                item(3, titles, 23000, "c"),
                item(4, titles, 24000, "c"),  # same seller as #3: collapses
                item(5, titles, 26000, "d"),
            ]
        }
    )
    report = await run_scan(shop, ebay, SETTINGS, limit=10, min_retail_cents=10000, now=T0)

    assert ebay.queries == ["ubiquiti UVC G5 Bullet"]  # pack single first; no $5 cable
    [single] = report.items
    assert (single.sellers, single.listings) == (4, 5)
    assert single.resale_cents == 22375  # p25 of 22000/22500/23000/26000
    assert single.net_cents > 0
    [pack] = report.breakouts
    assert pack.pack_sku == "UVC-G5-Bullet-3"
    assert pack.unit_cost_cents == 11967
    assert pack.breakeven_unit_cents == 15285  # exact $119.666.. unit cost, not rounded
    assert abs(pack.pack_net_cents - pack.unit_net_cents * 3) <= 2  # rounding only
    assert report.best_breakouts()[0].pack_sku == "UVC-G5-Bullet-3"

    async with shop() as session:
        for model in (EbayStats, Valuation, MarketComp, Event):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_market_service_scan_requires_ebay(shop):
    service = MarketService(shop, None, SETTINGS, None, clock=lambda: datetime.now(UTC))  # type: ignore[arg-type]
    with pytest.raises(Exception, match="not configured"):
        await service.scan()


async def test_collect_stores_distinct_seller_counts(shop):
    titles = "Ubiquiti G5 Bullet UVC-G5-Bullet"
    ebay = FakeEbay(
        {"ubiquiti UVC G5 Bullet": [item(n, titles, 22000 + n * 100, "same") for n in range(6)]}
    )

    class Dispatcher:
        async def dispatch(self) -> int:
            return 0

    service = MarketService(shop, ebay, SETTINGS, Dispatcher(), clock=lambda: T0)  # type: ignore[arg-type]
    async with shop() as session:
        vid = await session.scalar(select(EbayStats.variant_id).limit(1))
        from tracker.db.models import Variant

        vid = await session.scalar(select(Variant.id).where(Variant.sku == "UVC-G5-Bullet"))
    report = await service.collect(vid)
    assert report.matched == 6
    async with shop() as session:
        stats = await session.get(EbayStats, vid)
        assert (stats.active_listings, stats.active_count) == (6, 1)
        valuation = await session.get(Valuation, vid)
        # Six listings from one seller is one opinion: not enough to value.
        assert valuation.basis is None


def test_scan_is_owner_only():
    guild = GuildConfig(guild_id="1", status=GuildStatus.APPROVED, show_valuations=True)
    assert check_access("scan", Role.SERVER_ADMIN, guild) is not None
    assert check_access("scan", Role.OWNER, guild) is None
    assert StockStatus.IN_STOCK.is_available
