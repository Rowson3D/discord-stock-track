"""Discovery: identity, eBay matching and honest estimates, the pipeline, and Best Buy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from tracker.db.models import Candidate, CandidateHistory, CandidateSignal
from tracker.discovery.bestbuy import BestBuySource, availability
from tracker.discovery.identity import candidate_key, clean_title
from tracker.discovery.market import MarketReading, estimate, matches, score, summarize_items
from tracker.discovery.service import DiscoveryService, Signal
from tracker.domain import ListingSnapshot, SourceKey, StockStatus
from tracker.market.ebay import EbayItem
from tracker.settings import BestBuySettings, DiscoverySettings, MarketSettings

MARKET = MarketSettings(_env_file=None)  # type: ignore[call-arg]
SETTINGS = DiscoverySettings(_env_file=None)  # type: ignore[call-arg]
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
HEADPHONES = "Sony WH-1000XM5 Wireless Noise Canceling Headphones"


# ── identity ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "model", "upc", "key"),
    [
        (f"{HEADPHONES} $248 + Free Shipping", None, None, "model:wh1000xm5"),
        ("Sony - WH-1000XM5 Headphones - Black", "WH1000XM5/B", "027242923782", "model:wh1000xm5"),
        ("NVIDIA GeForce RTX 5090 Founders Edition 32GB GDDR7", None, None, "model:rtx5090"),
        ("Samsung 65in 4K 120Hz TV (2024)", None, None, "title:samsung-65in-4k-120hz-tv-2024"),
        ("Apple AirPods Pro 2 with USB-C", None, "195949052460", "upc:195949052460"),
        ("LEGO Star Wars Millennium Falcon 75192", None, None, "model:lego75192"),
    ],
)
def test_candidate_keys(title, model, upc, key):
    assert candidate_key(title, model=model, upc=upc) == key


def test_clean_title_drops_prices_and_shipping():
    assert clean_title(f"{HEADPHONES} $248 + Free Shipping") == HEADPHONES


# ── matching and estimates ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("SONY WH-1000XM5 Wireless Headphones Black NEW", True),
        ("Sony WH1000XM5/B Headphones", True),
        ("Case for Sony WH-1000XM5 hard shell", False),
        ("Sony WH-1000XM4 Headphones", False),
        ("Lot of 3 Sony WH-1000XM5", False),
        ("Sony WH-1000XM5 for parts", False),
    ],
)
def test_matches_model(title, ok):
    assert matches(title, HEADPHONES, "WH-1000XM5") is ok


def test_matches_rejects_bigger_sibling_models():
    gpu = "NVIDIA GeForce RTX 5090 Founders Edition"
    assert matches("NVIDIA RTX 5090 FE 32GB", gpu, "RTX 5090")
    assert not matches("RTX 5090 Ti", gpu, "RTX 5090")
    assert not matches("RTX 5080 FE", gpu, "RTX 5090")
    assert not matches("Gaming PC i9-14900K RTX 5090 64GB DDR5", gpu, "RTX 5090")
    assert not matches(
        "Skytech AMD RYZEN 9 9950X3D RTX 5090 64GB DDR5 RAM+4TB SSD", gpu, "RTX 5090"
    )
    assert not matches("Lenovo Legion Pro 7i 16in OLED RTX5090 32GB", gpu, "RTX 5090")
    assert not matches("Alphacool Core RTX 5090 Water Block with Backplate", gpu, "RTX 5090")
    assert matches("ASUS TUF Gaming GeForce RTX 5090 32GB GDDR7 OC", gpu, "RTX 5090")
    falcon = "LEGO Star Wars Millennium Falcon 75192"
    assert matches("LEGO 75192 UCS Millennium Falcon NEW SEALED", falcon, "LEGO 75192")
    assert not matches("LEGO 75192 Han Solo minifigure", falcon, "LEGO 75192")


def test_outliers_without_a_store_price():
    items = [item("LEGO 75192 Falcon", price, f"s{price}") for price in (27, 800, 850, 900, 9000)]
    *_, values = summarize_items(items, "LEGO Millennium Falcon 75192", "LEGO 75192", None)
    assert values == [80000, 85000, 90000]


def item(title: str, price: float, seller: str, shipping: float = 0) -> EbayItem:
    return EbayItem(
        item_id=f"{seller}-{price}",
        title=title,
        url=None,
        price_cents=round(price * 100),
        shipping_cents=round(shipping * 100),
        currency="USD",
        condition_id="1000",
        sold=False,
        seller=seller,
    )


def test_one_price_per_seller_with_shipping():
    items = [
        item("Sony WH-1000XM5 new", 300, "a"),
        item("Sony WH-1000XM5 sealed", 290, "a", shipping=5),  # a's cheapest: 295
        item("Sony WH-1000XM5", 310, "b"),
        item("Sony WH-1000XM5 case", 20, "c"),  # accessory: ignored
    ]
    matched, sellers, _, values = summarize_items(items, HEADPHONES, "WH-1000XM5", 29900)
    assert matched == 3
    assert sellers == 2
    assert values == [29500, 31000]


def reading(p25: int | None, sellers: int = 5) -> MarketReading:
    return MarketReading("q", 10, sellers, sellers, sellers, p25, p25, p25)


def test_estimate_caps_when_buyable_at_regular_price():
    # Trending at full price and buyable: eBay asks don't count above retail + tax.
    full = estimate(reading(40000), retail_cents=29900, regular_cents=None, available=True,
                    market=MARKET, settings=SETTINGS)  # fmt: skip
    assert full.capped
    assert full.net_cents < 0
    # On sale 30% off: resale judged against the regular price → a real margin.
    sale = estimate(reading(40000), retail_cents=24900, regular_cents=34900, available=True,
                    market=MARKET, settings=SETTINGS)  # fmt: skip
    assert sale.capped
    assert sale.net_cents > 0
    # Sold out at the store: the eBay market is real (medium confidence with 3+ sellers).
    gone = estimate(reading(40000), retail_cents=29900, regular_cents=None, available=False,
                    market=MARKET, settings=SETTINGS)  # fmt: skip
    assert not gone.capped
    assert gone.net_cents > 0
    assert gone.confidence == "medium"
    nothing = estimate(reading(None), retail_cents=29900, regular_cents=None, available=True,
                       market=MARKET, settings=SETTINGS)  # fmt: skip
    assert nothing.net_cents is None
    assert nothing.confidence == "none"


def test_score():
    assert score(None, None, 3, 5) == 0
    assert score(-500, -0.1, 3, 5) == 0
    assert score(5000, 0.25, 0, 5) > score(5000, 0.25, 0, 1)
    assert score(5000, 0.25, 5, 5) > score(5000, 0.25, 0, 5)


# ── pipeline ─────────────────────────────────────────────────────────────


class FakeEbay:
    def __init__(self, items: list[EbayItem]) -> None:
        self.items = items
        self.queries: list[str] = []

    async def search_active(self, query: str, **_: Any) -> list[EbayItem]:
        self.queries.append(query)
        return self.items


def sold_out_market() -> list[EbayItem]:
    return [item(f"Sony WH-1000XM5 #{n}", 450 + n * 5, f"s{n}") for n in range(5)]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def service(sessions, ebay=None, clock=None, **settings: Any) -> DiscoveryService:
    config = DiscoverySettings(_env_file=None, **settings)  # type: ignore[call-arg]
    return DiscoveryService(sessions, ebay, MARKET, config, clock=clock or Clock())


async def test_signals_from_different_sources_merge(sessions):
    discovery = service(sessions)
    created = await discovery.ingest(
        [
            Signal(source="manual", kind="manual", title=HEADPHONES),
            Signal(source="bestbuy", kind="trending", title="Sony - WH-1000XM5 - Black",
                   model="WH1000XM5/B", price_cents=29999, retail_source="bestbuy",
                   url="https://www.bestbuy.com/site/x.p?skuId=1", available=True, rank=1),
            Signal(source="bestbuy", kind="trending", title="Sony - WH-1000XM5 - Black",
                   model="WH1000XM5/B", rank=3),
        ]
    )  # fmt: skip
    assert created == 1
    async with sessions() as session:
        [candidate] = list(await session.scalars(select(Candidate)))
        assert candidate.key == "model:wh1000xm5"
        assert candidate.retail_cents == 29999
        assert candidate.retail_source == "bestbuy"
        assert candidate.signal_count == 3
        assert candidate.heat == pytest.approx(2.0 + 1.9 + 1.7)
        rows = list(await session.scalars(select(CandidateSignal)))
        assert {(r.source, r.kind, r.times_seen) for r in rows} == {
            ("manual", "manual", 1),
            ("bestbuy", "trending", 2),
        }


async def test_check_promotes_snapshots_and_trial_decides(sessions):
    clock = Clock()
    ebay = FakeEbay(sold_out_market())
    discovery = service(sessions, ebay, clock)
    await discovery.ingest(
        [Signal(source="bestbuy", kind="sold_out", title=HEADPHONES, price_cents=29999,
                available=False, retail_source="bestbuy")]
    )  # fmt: skip
    assert await discovery.check_due() == 1
    assert ebay.queries == ["WH-1000XM5"]
    async with sessions() as session:
        candidate = await session.scalar(select(Candidate))
        assert candidate.status == "promising"
        assert candidate.confidence == "medium"
        assert not candidate.capped
        assert candidate.net_cents > SETTINGS.min_profit_cents
        assert candidate.trial_until == NOW + timedelta(days=SETTINGS.trial_days)
        assert await session.scalar(select(func.count()).select_from(CandidateHistory)) == 1
    assert await discovery.check_due() == 0  # next check is tomorrow

    # The trial ends and the market collapsed: median of daily nets decides.
    ebay.items = [item("Sony WH-1000XM5", 250, f"t{n}") for n in range(5)]
    for day in range(1, SETTINGS.trial_days + 2):
        clock.now = NOW + timedelta(days=day, minutes=1)
        await discovery.check_due()
    async with sessions() as session:
        candidate = await session.scalar(select(Candidate))
        assert candidate.status == "dropped"

    # A fresh signal revives it.
    await discovery.ingest([Signal(source="bestbuy", kind="trending", title=HEADPHONES)])
    async with sessions() as session:
        assert (await session.scalar(select(Candidate))).status == "new"


async def test_buyable_full_price_is_never_promising(sessions):
    discovery = service(sessions, FakeEbay(sold_out_market()))
    await discovery.ingest(
        [Signal(source="bestbuy", kind="trending", title=HEADPHONES, price_cents=29999,
                available=True, retail_source="bestbuy")]
    )  # fmt: skip
    await discovery.check_due()
    async with sessions() as session:
        candidate = await session.scalar(select(Candidate))
    assert candidate.status == "checked"
    assert candidate.capped
    assert candidate.net_cents < 0


async def test_budget_and_ignore(sessions):
    ebay = FakeEbay(sold_out_market())
    discovery = service(sessions, ebay, daily_ebay_calls=1)
    await discovery.ingest(
        [
            Signal(source="manual", kind="manual", title=HEADPHONES),
            Signal(source="manual", kind="manual", title="NVIDIA RTX 5090 Founders Edition"),
        ]
    )
    assert await discovery.check_due() == 1  # budget allows one call today
    assert discovery.budget() == (1, 1)
    async with sessions() as session:
        unchecked = await session.scalar(select(Candidate).where(Candidate.checked_at.is_(None)))
    ignored = await discovery.decide(unchecked.id, "ignore")
    assert ignored.status == "ignored"
    async with sessions() as session:
        assert await discovery._due_ids(session, NOW + timedelta(days=30), 10) == []


async def test_manual_link_uses_store_price(sessions):
    async def fetch(url: str, source: SourceKey) -> ListingSnapshot:
        assert source is SourceKey.BESTBUY
        return ListingSnapshot(
            source=source, region="us", external_id="1", url=url, product_key="bb:1",
            product_name=HEADPHONES, brand="Sony", category=None, sku="1", variant_title=None,
            pack_size=1, status=StockStatus.OUT_OF_STOCK, raw_status="Sold Out",
            price_cents=29999, regular_price_cents=None, currency="USD",
            is_early_access=False, sold_out_at=None, restock_eta=None,
        )  # fmt: skip

    discovery = DiscoveryService(
        sessions, FakeEbay(sold_out_market()), MARKET, SETTINGS, fetch_listing=fetch,
        clock=Clock(),
    )  # fmt: skip
    candidate_id, message = await discovery.manual("https://www.bestbuy.com/site/sony/1.p")
    assert message is None
    async with sessions() as session:
        candidate = await session.get(Candidate, candidate_id)
    assert candidate.available is False
    assert candidate.retail_cents == 29999
    assert candidate.status == "promising"
    _, error = await discovery.manual("https://example.com/thing")
    assert error
    assert "link" in error


async def test_manual_without_ebay_saves_but_says_so(sessions):
    candidate_id, message = await service(sessions).manual("Steam Deck OLED 1TB")
    assert candidate_id is not None
    assert "eBay isn't configured" in message


# ── Best Buy ─────────────────────────────────────────────────────────────


def bestbuy_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    trending = {"results": [
        {"sku": "111", "rank": 1, "names": {"title": "Fallback name"}, "prices": {}},
        {"sku": "222", "rank": 2, "names": {"title": "Only in recs"},
         "prices": {"current": 50, "regular": 50}, "links": {"web": "https://www.bestbuy.com/2"}},
    ]}  # fmt: skip
    products = {"products": [{
        "sku": 111, "name": "Sony - WH-1000XM5 Wireless Headphones - Black",
        "upc": "027242923782", "modelNumber": "WH1000XM5/B", "manufacturer": "Sony",
        "salePrice": 279.99, "regularPrice": 399.99, "onSale": True, "orderable": "Available",
        "onlineAvailability": True, "url": "https://www.bestbuy.com/site/1.p",
        "categoryPath": [{"name": "Audio"}, {"name": "Headphones"}],
    }]}  # fmt: skip
    sold_out = {"products": [{
        "sku": 333, "name": "Nintendo Switch 2 Console", "salePrice": 449.99,
        "regularPrice": 449.99, "orderable": "SoldOut", "url": "https://www.bestbuy.com/3",
    }]}  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/v1/products/trendingViewed":
            return httpx.Response(404)  # falls back to /beta
        if path == "/beta/products/trendingViewed":
            return httpx.Response(200, json=trending)
        if path.endswith("/mostViewed"):
            return httpx.Response(500)
        if "orderable=SoldOut" in path:
            return httpx.Response(200, json=sold_out)
        if path.startswith("/v1/products(sku"):
            return httpx.Response(200, content=json.dumps(products))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_bestbuy_signals():
    calls: list[httpx.Request] = []
    settings = BestBuySettings(_env_file=None, api_key="test-key", categories="abcat0700000")  # type: ignore[call-arg]
    source = BestBuySource(settings, transport=bestbuy_transport(calls))
    try:
        signals = await source.signals()
    finally:
        await source.aclose()
    kinds = [(s.kind, s.title) for s in signals]
    assert ("trending", "Sony - WH-1000XM5 Wireless Headphones - Black") in kinds
    assert ("big_discount", "Sony - WH-1000XM5 Wireless Headphones - Black") in kinds  # 30% off
    assert ("trending", "Only in recs") in kinds  # recommendation fields as a fallback
    assert ("sold_out", "Nintendo Switch 2 Console") in kinds
    sony = next(s for s in signals if s.kind == "trending" and s.upc)
    assert sony.model == "WH1000XM5/B"
    assert sony.category == "Headphones"
    assert sony.price_cents == 27999
    assert sony.regular_cents == 39999
    assert sony.available
    switch = next(s for s in signals if s.kind == "sold_out")
    assert switch.available is False
    assert all(r.url.params["apiKey"] == "test-key" for r in calls)


def test_bestbuy_availability():
    assert availability({"onlineAvailability": False, "orderable": "Available"}) is False
    assert availability({"orderable": "SoldOut"}) is False
    assert availability({"orderable": "PreOrder"}) is True
    assert availability({}) is None
