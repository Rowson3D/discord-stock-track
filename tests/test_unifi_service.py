from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.db.models import Event, Listing, Watch
from tracker.domain import EventKind, ListingSnapshot, StockStatus
from tracker.services.unifi import UnifiService, slug_from_url
from tracker.settings import MonitorSettings
from tracker.sources.unifi.parser import FreshlyStocked, HomePage
from tracker.sources.unifi.store import CrawlResult


def product(slug: str, **overrides: object) -> ListingSnapshot:
    values: dict[str, object] = {
        "external_id": f"{slug.upper()}-US",
        "sku": slug.upper(),
        "url": f"https://store.ui.com/us/en/products/{slug}",
        "product_key": f"ubiquiti:{slug}",
        "product_name": slug,
    }
    values.update(overrides)
    return make_snapshot(**values)


class FakeStore:
    def __init__(self) -> None:
        self.catalog: list[ListingSnapshot] = []
        self.errors: dict[str, str] = {}
        self.home = HomePage(region="us")
        self.products: dict[str, list[ListingSnapshot]] = {}
        self.product_calls: list[tuple[str, str]] = []

    async def crawl_catalog(self, region: str = "us") -> CrawlResult:
        return CrawlResult(region=region, snapshots=list(self.catalog), errors=dict(self.errors))

    async def fetch_home(self, region: str = "us") -> HomePage:
        return self.home

    async def fetch_product(self, slug: str, region: str = "us") -> list[ListingSnapshot]:
        self.product_calls.append((region, slug))
        return self.products.get(slug, [])


class CountingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self) -> int:
        self.calls += 1
        return 0


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def service(sessions: async_sessionmaker[AsyncSession], store: FakeStore, clock: Clock):
    settings = MonitorSettings(_env_file=None, hot_products_per_tick=10)  # type: ignore[call-arg]
    return UnifiService(sessions, store, settings, CountingDispatcher(), clock=clock)


async def _events(sessions: async_sessionmaker[AsyncSession]) -> list[tuple[str, str | None]]:
    async with sessions() as session:
        rows = await session.scalars(select(Event).order_by(Event.id))
        return [(e.kind, e.new_value) for e in rows]


async def test_delist_after_grace_then_relist(service, store, clock, sessions):
    store.catalog = [product("a"), product("b")]
    await service.crawl("us")

    store.catalog = [product("a")]
    clock.advance(minutes=10)
    stats = await service.crawl("us")
    assert stats.delisted == 0  # still within the grace period

    clock.advance(minutes=50)
    stats = await service.crawl("us")
    assert stats.delisted == 1
    async with sessions() as session:
        gone = await session.scalar(select(Listing).where(Listing.external_id == "B-US"))
        assert gone.delisted_at == clock.now

    store.catalog = [product("a"), product("b")]
    clock.advance(minutes=5)
    await service.crawl("us")
    assert await _events(sessions) == [(EventKind.DELISTED, None), (EventKind.RELISTED, "B")]


async def test_incomplete_crawl_never_delists(service, store, clock):
    store.catalog = [product("a"), product("b")]
    await service.crawl("us")
    store.catalog = [product("a")]
    store.errors = {"all-wifi": "HTTP 503"}
    clock.advance(hours=2)
    stats = await service.crawl("us")
    assert not stats.complete
    assert stats.delisted == 0


async def test_low_coverage_crawl_never_delists(service, store, clock):
    store.catalog = [product(s) for s in "abcd"]
    await service.crawl("us")
    store.catalog = [product("a")]  # 1 of 4: looks like a store glitch, not 3 delistings
    clock.advance(hours=2)
    assert (await service.crawl("us")).delisted == 0


async def test_new_listing_is_queued_for_product_poll(service, store, clock):
    store.catalog = [product("a")]
    await service.crawl("us")
    store.catalog = [product("a"), product("new-ap")]
    store.products["new-ap"] = [product("new-ap", is_early_access=True)]
    clock.advance(minutes=5)
    await service.crawl("us")

    assert service.pending_count() == 1
    await service.poll_hot()
    assert store.product_calls == [("us", "new-ap")]
    assert service.pending_count() == 0


async def test_home_feeds_queue_each_restock_once(service, store, clock):
    stocked = FreshlyStocked("U7-Pro-US", "u7-pro", T0, is_new=False)
    store.home = HomePage(region="us", freshly_stocked=[stocked], whats_new_slugs=["e7"])

    assert await service.check_home("us") == 2
    assert await service.check_home("us") == 0

    restocked_again = FreshlyStocked("U7-Pro-US", "u7-pro", T0 + timedelta(hours=1), False)
    store.home = HomePage(region="us", freshly_stocked=[restocked_again])
    assert await service.check_home("us") == 1
    assert service.pending_count() == 2  # u7-pro is queued once, plus e7


async def test_hot_listing_selection(service, store, clock, sessions):
    store.catalog = [
        product("plain"),
        product("priority"),
        product("watched"),
        product("soon", status=StockStatus.COMING_SOON),
        product("eta", status=StockStatus.OUT_OF_STOCK, restock_eta=date(2026, 11, 9)),
        product("far-eta", status=StockStatus.OUT_OF_STOCK, restock_eta=date(2027, 1, 1)),
        product("flipped"),
    ]
    await service.crawl("us")
    async with sessions() as session, session.begin():
        listings = {
            listing.external_id: listing for listing in await session.scalars(select(Listing))
        }
        listings["PRIORITY-US"].priority = "high"
        session.add(Watch(discord_user_id="1", listing_id=listings["WATCHED-US"].id))

    # "flipped" sells out: recently changed listings stay hot for a while.
    clock.advance(minutes=1)
    store.catalog[-1] = product("flipped", status=StockStatus.OUT_OF_STOCK)
    await service.crawl("us")

    # Nothing is due right after a crawl touched every listing.
    assert await service.due_hot_products(20) == []

    clock.advance(minutes=2)
    due = {slug for _, slug in await service.due_hot_products(20)}
    assert due == {"priority", "watched", "soon", "eta", "flipped"}


def test_slug_from_url():
    assert slug_from_url("https://store.ui.com/us/en/products/u7-pro") == "u7-pro"
    assert slug_from_url("https://ca.store.ui.com/ca/en/products/u7-pro?x=1") == "u7-pro"
    assert slug_from_url("https://www.amazon.com/dp/B0ABCDEFGH") is None
