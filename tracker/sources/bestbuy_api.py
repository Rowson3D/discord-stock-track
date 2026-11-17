"""Best Buy stock checks through the official Products API (free key, developer.bestbuy.com).

Best Buy refuses automated page fetches, so with a key, Best Buy listings are checked here
instead: one small JSON request per product, within the API's published limits (5 calls a
second, 50,000 a day). Snapshots have the same identity as page-based ones (`BBY-<sku>`),
so switching between the two never duplicates a listing.

The key travels as the `apiKey` query parameter, as the API requires; it's a registered
secret, so log redaction masks it, and request URLs are never logged here.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import httpx

from tracker.discovery.bestbuy import availability, category_of, cents
from tracker.domain import ListingSnapshot, SourceKey, StockStatus
from tracker.http import FetchError
from tracker.settings import BestBuySettings
from tracker.sources.web import bestbuy_sku, url_snapshot

logger = logging.getLogger(__name__)

SHOW = (
    "sku,name,manufacturer,salePrice,regularPrice,onSale,orderable,onlineAvailability,"
    "categoryPath.name"
)


def stock_status(product: dict[str, Any]) -> StockStatus:
    orderable = str(product.get("orderable") or "").lower()
    if orderable == "comingsoon":
        return StockStatus.COMING_SOON
    available = availability(product)
    if available is True:
        return StockStatus.IN_STOCK
    if available is False:
        return StockStatus.OUT_OF_STOCK
    return StockStatus.UNKNOWN


def api_snapshot(url: str, product: dict[str, Any]) -> ListingSnapshot:
    price = cents(product.get("salePrice"))
    regular = cents(product.get("regularPrice"))
    snapshot = url_snapshot(
        url,
        SourceKey.BESTBUY,
        name=str(product.get("name") or "") or None,
        status=stock_status(product),
        price_cents=price,
        brand=str(product.get("manufacturer") or "") or None,
        category=category_of(product),
    )
    on_sale = bool(product.get("onSale")) and regular is not None and price is not None
    return dataclasses.replace(
        snapshot,
        regular_price_cents=regular if on_sale and regular > price else None,  # type: ignore[operator]
        raw_status=f"api:{product.get('orderable') or 'unknown'}",
    )


class BestBuyStock:
    def __init__(
        self, settings: BestBuySettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.api_base.rstrip("/"),
            timeout=15,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "FlipScout/1.0 (private use)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def snapshot(self, url: str) -> ListingSnapshot:
        """Raises FetchError for anything that isn't a usable product answer."""
        sku = bestbuy_sku(url)
        if sku is None:
            raise FetchError(url, "no Best Buy SKU in this link")
        key = self._settings.api_key.get_secret_value() if self._settings.api_key else ""
        try:
            response = await self._client.get(
                f"/v1/products/{sku}.json", params={"apiKey": key, "show": SHOW}
            )
        except httpx.HTTPError as exc:
            raise FetchError(url, f"Best Buy API unreachable: {type(exc).__name__}") from exc
        if response.status_code == 404:
            raise FetchError(url, f"Best Buy has no product {sku}", 404)
        if response.status_code == 403:
            raise FetchError(url, "Best Buy API refused the key or the daily limit is used", 403)
        if response.status_code != 200:
            raise FetchError(url, f"Best Buy API HTTP {response.status_code}", response.status_code)
        try:
            product = response.json()
        except ValueError as exc:
            raise FetchError(url, "Best Buy API sent invalid JSON") from exc
        if not isinstance(product, dict) or not product.get("sku"):
            raise FetchError(url, "Best Buy API sent no product")
        return api_snapshot(url, product)
