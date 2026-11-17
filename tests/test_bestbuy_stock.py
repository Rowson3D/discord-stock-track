"""Best Buy stock checks through the official Products API."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from tracker.app import build_retail_fetch
from tracker.db.models import Event, Listing
from tracker.domain import EventKind, SourceKey, StockStatus
from tracker.http import FetchError
from tracker.services.retail import RetailService
from tracker.settings import BestBuySettings, Settings
from tracker.sources.bestbuy_api import BestBuyStock, api_snapshot, stock_status
from tracker.sources.web import bestbuy_sku, listing_sku, url_snapshot

URL = "https://www.bestbuy.com/site/6614151.p?skuId=6614151"
FE = {
    "sku": 6614151,
    "name": "NVIDIA - GeForce RTX 5090 32GB GDDR7 Founders Edition",
    "manufacturer": "NVIDIA",
    "salePrice": 1999.99,
    "regularPrice": 1999.99,
    "onSale": False,
    "orderable": "SoldOut",
    "onlineAvailability": False,
    "categoryPath": [{"name": "Computers & Tablets"}, {"name": "GPUs / Video Graphics Cards"}],
}


@pytest.mark.parametrize(
    ("url", "sku"),
    [
        (URL, "6614151"),
        ("https://www.bestbuy.com/site/6614153.p", "6614153"),
        ("https://www.bestbuy.com/site/nvidia-rtx-5070/6614154.p?skuId=6614154", "6614154"),
        ("https://www.bestbuy.com/product/nvidia-rtx-5090/J3GWYHGPCP/sku/6614151", "6614151"),
        ("https://www.bestbuy.com/product/nvidia-rtx-5090/6614151", "6614151"),
        ("https://www.bestbuy.com/product/playstation-5-pro-console/JXHQ37TR86", None),
        ("https://www.bestbuy.com/site/searchpage.jsp?st=gpu", None),
    ],
)
def test_sku_from_any_link(url, sku):
    assert bestbuy_sku(url) == sku


def test_same_identity_as_page_checks():
    other_form = "https://www.bestbuy.com/site/6614151.p"
    assert listing_sku(URL, SourceKey.BESTBUY) == "BBY-6614151"
    assert listing_sku(other_form, SourceKey.BESTBUY) == "BBY-6614151"
    snap = api_snapshot(URL, FE)
    assert snap.sku == url_snapshot(URL, SourceKey.BESTBUY).sku == "BBY-6614151"
    assert snap.product_key == "bestbuy:bby-6614151"


def test_status_mapping_and_prices():
    assert stock_status(FE) is StockStatus.OUT_OF_STOCK
    assert stock_status({**FE, "onlineAvailability": True, "orderable": "Available"}) is (
        StockStatus.IN_STOCK
    )
    assert stock_status({**FE, "onlineAvailability": None, "orderable": "ComingSoon"}) is (
        StockStatus.COMING_SOON
    )
    assert stock_status({"sku": 1}) is StockStatus.UNKNOWN
    snap = api_snapshot(URL, FE)
    assert snap.product_name == FE["name"]
    assert snap.brand == "NVIDIA"
    assert snap.category == "GPUs / Video Graphics Cards"
    assert snap.price_cents == 199999
    assert snap.regular_price_cents is None
    assert snap.raw_status == "api:SoldOut"
    sale = api_snapshot(URL, {**FE, "salePrice": 1799.99, "onSale": True})
    assert (sale.price_cents, sale.regular_price_cents) == (179999, 199999)


def stock(handler) -> BestBuyStock:
    settings = BestBuySettings(_env_file=None, api_key="test-key")  # type: ignore[call-arg]
    return BestBuyStock(settings, httpx.MockTransport(handler))


async def test_api_request_and_answer():
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={**FE, "onlineAvailability": True, "orderable": "Available"}
        )

    client = stock(handle)
    snap = await client.snapshot(URL)
    await client.aclose()
    assert snap.status is StockStatus.IN_STOCK
    [request] = seen
    assert request.url.path == "/v1/products/6614151.json"
    assert request.url.params["apiKey"] == "test-key"
    assert "onlineAvailability" in request.url.params["show"]


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (404, {}, "no product 6614151"),
        (403, {}, "refused the key"),
        (500, {}, "HTTP 500"),
        (200, {"error": "x"}, "sent no product"),
    ],
)
async def test_api_failures_are_fetch_errors(status, body, message):
    client = stock(lambda request: httpx.Response(status, json=body))
    with pytest.raises(FetchError, match=message):
        await client.snapshot(URL)
    await client.aclose()


async def test_unusable_links_and_network_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = stock(boom)
    with pytest.raises(FetchError, match="no Best Buy SKU"):
        await client.snapshot("https://www.bestbuy.com/site/searchpage.jsp?st=gpu")
    with pytest.raises(FetchError, match="unreachable"):
        await client.snapshot(URL)
    await client.aclose()


async def test_retail_fetch_routing():
    settings = Settings(env_file=None)
    _, api = build_retail_fetch(settings, http=None)  # type: ignore[arg-type]
    assert api is None  # no key: page checks as before
    settings.bestbuy = BestBuySettings(_env_file=None, api_key="k", stock_interval_seconds=20)  # type: ignore[call-arg]
    _, api = build_retail_fetch(settings, http=None)  # type: ignore[arg-type]
    assert isinstance(api, BestBuyStock)
    assert settings.retail.bestbuy_interval_seconds == 20
    await api.aclose()


class NoDispatch:
    async def dispatch(self) -> int:
        return 0


async def test_restock_through_the_api_creates_an_alert(sessions):
    answers = iter([FE, {**FE, "onlineAvailability": True, "orderable": "Available"}])
    client = stock(lambda request: httpx.Response(200, content=json.dumps(next(answers))))

    async def fetch(url: str, source: SourceKey):
        return await client.snapshot(url)

    retail = RetailService(sessions, fetch, Settings(env_file=None).retail, NoDispatch())
    listing_id, snap = await retail.add_url(URL, SourceKey.BESTBUY)
    assert snap.status is StockStatus.OUT_OF_STOCK
    assert await retail.poll(listing_id, URL, SourceKey.BESTBUY)
    await client.aclose()
    async with sessions() as session:
        listing = await session.get(Listing, listing_id)
        kinds = list(
            await session.scalars(select(Event.kind).where(Event.listing_id == listing_id))
        )
    assert listing.status == StockStatus.IN_STOCK.value
    assert EventKind.BACK_IN_STOCK.value in kinds
