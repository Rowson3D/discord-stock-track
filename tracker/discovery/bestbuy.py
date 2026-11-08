"""Best Buy Products API source (official, free key from developer.bestbuy.com).

Signals:
- `trending` and `most_viewed`: the Recommendations endpoints (top products by rank).
- `big_discount`: one of those on sale at 20% or more under regular price.
- `sold_out`: sold-out products in the categories you list (`BESTBUY_CATEGORIES`),
  best sellers first. These are restock targets.

The key is sent as the `apiKey` query parameter, as the API requires. It's registered as a
secret so log redaction masks it, and URLs are never logged here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tracker.discovery.service import Signal
from tracker.settings import BestBuySettings

logger = logging.getLogger(__name__)

SHOW = (
    "sku,name,upc,modelNumber,manufacturer,salePrice,regularPrice,onSale,orderable,"
    "onlineAvailability,url,categoryPath.name"
)
RECOMMENDATIONS = {"trendingViewed": "trending", "mostViewed": "most_viewed"}
BIG_DISCOUNT = 0.20


def cents(value: Any) -> int | None:
    try:
        return round(float(value) * 100) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def availability(product: dict[str, Any]) -> bool | None:
    online = product.get("onlineAvailability")
    if isinstance(online, bool):
        return online
    orderable = str(product.get("orderable") or "").lower()
    if orderable in ("available", "preorder"):
        return True
    if orderable in ("soldout", "notavailable", "comingsoon"):
        return False
    return None


def category_of(product: dict[str, Any]) -> str | None:
    path = product.get("categoryPath") or []
    if isinstance(path, list) and path:
        last = path[-1]
        return str(last.get("name"))[:200] if isinstance(last, dict) else None
    return None


def product_signal(product: dict[str, Any], kind: str, rank: int | None = None) -> Signal | None:
    name = str(product.get("name") or "").strip()
    if not name:
        return None
    return Signal(
        source="bestbuy",
        kind=kind,
        title=name,
        url=product.get("url"),
        price_cents=cents(product.get("salePrice")),
        regular_cents=cents(product.get("regularPrice")),
        upc=str(product.get("upc") or "") or None,
        model=str(product.get("modelNumber") or "") or None,
        brand=str(product.get("manufacturer") or "") or None,
        category=category_of(product),
        available=availability(product),
        rank=rank,
        retail_source="bestbuy",
    )


def recommendation_fallback(item: dict[str, Any]) -> dict[str, Any]:
    """A product-shaped dict from a recommendations entry (when details aren't available)."""
    prices = item.get("prices") or {}
    links = item.get("links") or {}
    names = item.get("names") or {}
    return {
        "sku": item.get("sku"),
        "name": names.get("title"),
        "salePrice": prices.get("current"),
        "regularPrice": prices.get("regular"),
        "url": links.get("web"),
    }


class BestBuySource:
    name = "bestbuy"

    def __init__(
        self, settings: BestBuySettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.api_base.rstrip("/"),
            timeout=20,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "FlipScout/1.0 (private use)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _key(self) -> str:
        return self._settings.api_key.get_secret_value() if self._settings.api_key else ""

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {"apiKey": self._key(), "format": "json", **(params or {})}
        response = await self._client.get(path, params=query)
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"Best Buy {path.split('(')[0]} returned HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        return response.json()

    async def recommendations(self, endpoint: str) -> list[dict[str, Any]]:
        # Documented under /v1; older docs used /beta. Try both before giving up.
        for prefix in ("/v1", "/beta"):
            try:
                payload = await self._get(f"{prefix}/products/{endpoint}")
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    continue
                raise
            return list(payload.get("results") or [])
        return []

    async def products(
        self, query: str, *, sort: str | None = None, page_size: int = 50
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"show": SHOW, "pageSize": page_size}
        if sort:
            params["sort"] = sort
        payload = await self._get(f"/v1/products({query})", params)
        return list(payload.get("products") or [])

    async def details(self, skus: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(skus), 50):
            chunk = [s for s in skus[start : start + 50] if s.isdigit()]
            if chunk:
                for product in await self.products(f"sku in({','.join(chunk)})"):
                    found[str(product.get("sku"))] = product
        return found

    async def signals(self) -> list[Signal]:
        out: list[Signal] = []
        ranked: list[tuple[str, int | None, dict[str, Any]]] = []
        for endpoint, kind in RECOMMENDATIONS.items():
            try:
                for index, item in enumerate(await self.recommendations(endpoint)):
                    rank = item.get("rank") if isinstance(item.get("rank"), int) else index + 1
                    ranked.append((kind, rank, item))
            except httpx.HTTPError as error:
                logger.warning("Best Buy %s unavailable: %s", endpoint, error)
        skus = sorted({str(item.get("sku")) for _, _, item in ranked if item.get("sku")})
        try:
            details = await self.details(skus)
        except httpx.HTTPError as error:
            logger.warning("Best Buy product details unavailable: %s", error)
            details = {}
        for kind, rank, item in ranked:
            product = details.get(str(item.get("sku"))) or recommendation_fallback(item)
            signal = product_signal(product, kind, rank)
            if signal is None:
                continue
            out.append(signal)
            sale, regular = signal.price_cents, signal.regular_cents
            if sale and regular and sale <= regular * (1 - BIG_DISCOUNT):
                discounted = product_signal(product, "big_discount", rank)
                if discounted is not None:
                    out.append(discounted)
        for category in self._settings.categories:
            if not category.replace("_", "").isalnum():
                logger.warning("Skipping odd Best Buy category id %r", category)
                continue
            try:
                sold_out = await self.products(
                    f"categoryPath.id={category}&orderable=SoldOut",
                    sort="bestSellingRank.asc",
                    page_size=25,
                )
            except httpx.HTTPError as error:
                logger.warning("Best Buy sold-out scan for %s failed: %s", category, error)
                continue
            for index, product in enumerate(sold_out):
                signal = product_signal(product, "sold_out", index + 1)
                if signal is not None:
                    out.append(signal)
        return out
