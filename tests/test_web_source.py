"""URL-retailer parsing. Block pages are real (captured 2026-11-07); product pages are
synthetic schema.org markup because every retailer blocked our capture attempts."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0
from tracker.db.models import Listing
from tracker.domain import ListingSnapshot, SourceKey, StockStatus
from tracker.http import FetchError
from tracker.services.retail import RetailService, effective_interval
from tracker.settings import RetailSettings
from tracker.sources.web import (
    BlockedError,
    detect_source,
    listing_sku,
    map_availability,
    parse_money,
    parse_retail_page,
    url_snapshot,
)

BBY = "https://www.bestbuy.com/site/some-gpu/6521430.p?skuId=6521430"
AMZN = "https://www.amazon.com/Ubiquiti-U7-Pro/dp/B0ABCDEFGH/ref=sr_1_1"


def _ld(payload: object) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps(payload)
        + "</script></head><body><h1>Page title</h1></body></html>"
    )


def test_jsonld_product_with_offer():
    html = _ld(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "NVIDIA GeForce RTX 4090",
            "brand": {"@type": "Brand", "name": "NVIDIA"},
            "offers": {
                "@type": "Offer",
                "price": "1599.99",
                "priceCurrency": "USD",
                "availability": "https://schema.org/InStock",
            },
        }
    )
    snap = parse_retail_page(html, BBY, SourceKey.BESTBUY)
    assert snap.status is StockStatus.IN_STOCK
    assert snap.price_cents == 159999
    assert snap.product_name == "NVIDIA GeForce RTX 4090"
    assert snap.brand == "NVIDIA"
    assert snap.sku == "BBY-6521430"
    assert snap.external_id == BBY


def test_jsonld_graph_and_aggregate_offer_prefers_available_offer():
    html = _ld(
        {
            "@graph": [
                {"@type": "BreadcrumbList"},
                {
                    "@type": ["Product"],
                    "name": "Thing",
                    "offers": {
                        "@type": "AggregateOffer",
                        "lowPrice": 99,
                        "offers": [
                            {"availability": "http://schema.org/OutOfStock", "price": 99},
                            {"availability": "http://schema.org/InStock", "price": 120},
                        ],
                    },
                },
            ]
        }
    )
    snap = parse_retail_page(html, BBY, SourceKey.BESTBUY)
    assert snap.status is StockStatus.IN_STOCK
    assert snap.price_cents == 9900


def test_amazon_fallback_without_jsonld():
    html = """<html><body><span id="productTitle"> U7 Pro </span>
    <span class="a-price"><span class="a-offscreen">$189.00</span></span>
    <div id="availability"><span>Only 3 left in stock - order soon.</span></div></body></html>"""
    snap = parse_retail_page(html, AMZN, SourceKey.AMAZON)
    assert snap.status is StockStatus.LOW_STOCK
    assert snap.price_cents == 18900
    assert snap.product_name == "U7 Pro"
    assert snap.sku == "AMZN-B0ABCDEFGH"


def test_buy_box_fallback_ignores_text_elsewhere_on_page():
    html = """<html><body><h1>Card</h1>
    <div class="fulfillment-add-to-cart-button"><button>Sold Out</button></div>
    <div class="recommendations">Other card <button>Add to Cart</button></div></body></html>"""
    assert parse_retail_page(html, BBY, SourceKey.BESTBUY).status is StockStatus.OUT_OF_STOCK


def test_real_amazon_captcha_page_is_blocked(fixture_text: Callable[[str], str]):
    with pytest.raises(BlockedError):
        parse_retail_page(fixture_text("retail/amazon_captcha.html"), AMZN, SourceKey.AMAZON)


def test_real_cloudflare_block_page_is_blocked(fixture_text: Callable[[str], str]):
    with pytest.raises(BlockedError):
        parse_retail_page(
            fixture_text("retail/newegg_403.html"),
            "https://www.newegg.com/p/N82E16814137771",
            SourceKey.NEWEGG,
        )


@pytest.mark.parametrize(
    ("url", "source"),
    [
        ("https://store.ui.com/us/en/products/u7-pro", SourceKey.UNIFI),
        ("https://ca.store.ui.com/ca/en/products/u7-pro", SourceKey.UNIFI),
        ("https://www.amazon.com/dp/B0ABCDEFGH", SourceKey.AMAZON),
        ("https://smile.amazon.com/dp/B0ABCDEFGH", SourceKey.AMAZON),
        ("https://www.bestbuy.com/site/x/1.p", SourceKey.BESTBUY),
        ("https://www.bhphotovideo.com/c/product/1810927-REG/x.html", SourceKey.BHPHOTO),
        ("https://www.newegg.com/p/N82E1", SourceKey.NEWEGG),
        ("https://amazon.com.evil.example/dp/B0ABCDEFGH", None),
        ("https://notbestbuy.com/x", None),
        ("https://help.ui.com/hc/en-us", None),
    ],
)
def test_detect_source(url: str, source: SourceKey | None):
    assert detect_source(url) is source


def test_listing_sku():
    assert listing_sku(AMZN, SourceKey.AMAZON) == "AMZN-B0ABCDEFGH"
    assert listing_sku(BBY, SourceKey.BESTBUY) == "BBY-6521430"
    bh = "https://www.bhphotovideo.com/c/product/1810927-REG/ubiquiti.html"
    assert listing_sku(bh, SourceKey.BHPHOTO) == "BH-1810927"
    assert listing_sku("https://www.newegg.com/p/n82e1", SourceKey.NEWEGG) == "NEWEGG-N82E1"
    assert listing_sku("x", SourceKey.BESTBUY, explicit="RTX-4090-FE") == "RTX-4090-FE"


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        ("$189.00", 18900),
        ("$1,299.99", 129999),
        ("$5", 500),
        ("$5.5", 550),
        (1599.99, 159999),
        (0, None),
        (None, None),
        ("", None),
    ],
)
def test_parse_money(value: object, cents: int | None):
    assert parse_money(value) == cents


def test_map_availability():
    assert map_availability("https://schema.org/PreOrder") is StockStatus.COMING_SOON
    assert map_availability("LimitedAvailability") is StockStatus.LOW_STOCK
    assert map_availability("http://schema.org/BackOrder") is StockStatus.OUT_OF_STOCK
    assert map_availability("weird") is StockStatus.UNKNOWN


class FakeDispatcher:
    async def dispatch(self) -> int:
        return 0


class FakeFetch:
    def __init__(self) -> None:
        self.result: ListingSnapshot | Exception = url_snapshot(BBY, SourceKey.BESTBUY)
        self.calls: list[str] = []

    async def __call__(self, url: str, source: SourceKey) -> ListingSnapshot:
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_retail_service_polls_due_and_backs_off(sessions: async_sessionmaker[AsyncSession]):
    now = [T0]
    fetch = FakeFetch()
    settings = RetailSettings(_env_file=None)  # type: ignore[call-arg]
    service = RetailService(sessions, fetch, settings, FakeDispatcher(), clock=lambda: now[0])

    fetch.result = BlockedError("captcha")
    listing_id, _ = await service.add_url(BBY, SourceKey.BESTBUY)
    assert await service.due_listings() == []  # just checked

    now[0] += timedelta(seconds=121)
    assert [d[0] for d in await service.due_listings()] == [listing_id]
    for _ in range(10):
        await service.poll(listing_id, BBY, SourceKey.BESTBUY)
    async with sessions() as session:
        listing = await session.get(Listing, listing_id)
        assert listing.consecutive_unknowns == 11  # the initial add_url fetch counts too
        assert effective_interval(settings, listing) == 1200  # 120s x 10 backoff

    fetch.result = url_snapshot(BBY, SourceKey.BESTBUY, status=StockStatus.IN_STOCK, price_cents=1)
    now[0] += timedelta(hours=1)
    assert await service.poll_due() == 1
    async with sessions() as session:
        listing = await session.scalar(select(Listing))
        assert listing.status == StockStatus.IN_STOCK
        assert listing.consecutive_unknowns == 0


async def test_add_url_survives_fetch_failure(sessions: async_sessionmaker[AsyncSession]):
    fetch = FakeFetch()
    fetch.result = FetchError(BBY, "HTTP 403", 403)
    settings = RetailSettings(_env_file=None)  # type: ignore[call-arg]
    service = RetailService(sessions, fetch, settings, FakeDispatcher(), clock=lambda: T0)
    listing_id, snapshot = await service.add_url(BBY, SourceKey.BESTBUY)
    assert listing_id > 0
    assert snapshot.status is StockStatus.UNKNOWN
