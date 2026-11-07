"""eBay official APIs (application token, no user login).

* Browse API `item_summary/search`: active listings. Available to every developer app.
* Marketplace Insights `item_sales/search`: sold listings (last 90 days). Limited release:
  eBay must approve the app. A 403 disables it for the rest of the process.

Docs: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
      https://developer.ebay.com/api-docs/buy/marketplace-insights/resources/item_sales/methods/search
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from tracker.settings import EbaySettings
from tracker.sources.web import parse_money

logger = logging.getLogger(__name__)

BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"


class EbayError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class InsightsUnavailable(EbayError):
    """The app isn't approved for Marketplace Insights (sold data)."""


@dataclass(frozen=True, slots=True)
class EbayItem:
    item_id: str
    title: str
    url: str | None
    price_cents: int
    shipping_cents: int | None
    currency: str
    condition_id: str | None
    sold: bool
    sold_at: datetime | None = None
    sold_quantity: int = 1


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _shipping(item: dict[str, Any]) -> int | None:
    options = item.get("shippingOptions") or []
    costs = [
        parse_money((o.get("shippingCost") or {}).get("value")) or 0
        for o in options
        if isinstance(o, dict) and o.get("shippingCost")
    ]
    return min(costs) if costs else None


def parse_active(payload: dict[str, Any]) -> list[EbayItem]:
    items: list[EbayItem] = []
    for item in payload.get("itemSummaries") or []:
        price = item.get("price") or {}
        cents = parse_money(price.get("value"))
        if not item.get("itemId") or not cents:
            continue
        items.append(
            EbayItem(
                item_id=str(item["itemId"]),
                title=str(item.get("title") or ""),
                url=item.get("itemWebUrl"),
                price_cents=cents,
                shipping_cents=_shipping(item),
                currency=str(price.get("currency") or "USD"),
                condition_id=item.get("conditionId"),
                sold=False,
            )
        )
    return items


def parse_sold(payload: dict[str, Any]) -> list[EbayItem]:
    items: list[EbayItem] = []
    for item in payload.get("itemSales") or []:
        price = item.get("lastSoldPrice") or {}
        cents = parse_money(price.get("value"))
        if not item.get("itemId") or not cents:
            continue
        items.append(
            EbayItem(
                item_id=str(item["itemId"]),
                title=str(item.get("title") or ""),
                url=item.get("itemWebUrl"),
                price_cents=cents,
                shipping_cents=_shipping(item),
                currency=str(price.get("currency") or "USD"),
                condition_id=item.get("conditionId"),
                sold=True,
                sold_at=_parse_time(item.get("lastSoldDate")),
                sold_quantity=int(item.get("totalSoldQuantity") or 1),
            )
        )
    return items


class EbayClient:
    def __init__(
        self, settings: EbaySettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.api_base, timeout=20, transport=transport
        )
        self._tokens: dict[str, tuple[str, float]] = {}
        self.insights_enabled = settings.use_marketplace_insights

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token(self, scope: str) -> str:
        cached = self._tokens.get(scope)
        if cached and cached[1] > time.monotonic() + 60:
            return cached[0]
        secret = (
            self._settings.client_secret.get_secret_value() if self._settings.client_secret else ""
        )
        basic = base64.b64encode(f"{self._settings.client_id}:{secret}".encode()).decode()
        response = await self._client.post(
            "/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": scope},
        )
        if response.status_code != 200:
            raise EbayError(
                f"token request failed: HTTP {response.status_code}", response.status_code
            )
        body = response.json()
        token = body["access_token"]
        self._tokens[scope] = (token, time.monotonic() + float(body.get("expires_in", 7200)))
        return token

    async def _get(self, path: str, scope: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._token(scope)
            response = await self._client.get(
                path,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": self._settings.marketplace,
                },
            )
            if response.status_code == 401 and attempt == 0:
                self._tokens.pop(scope, None)  # expired or revoked: fetch a new token once
                continue
            if response.status_code == 403 and scope == INSIGHTS_SCOPE:
                raise InsightsUnavailable("app not approved for Marketplace Insights", 403)
            if response.status_code != 200:
                raise EbayError(
                    f"GET {path} failed: HTTP {response.status_code}", response.status_code
                )
            return response.json()
        raise EbayError(f"GET {path} unauthorized", 401)

    async def search_active(
        self, query: str, *, min_price_cents: int | None = None, limit: int = 100
    ) -> list[EbayItem]:
        filters = ["buyingOptions:{FIXED_PRICE}", "conditionIds:{1000|1500}"]
        if min_price_cents:
            filters += [f"price:[{min_price_cents / 100:.2f}..]", "priceCurrency:USD"]
        payload = await self._get(
            "/buy/browse/v1/item_summary/search",
            BROWSE_SCOPE,
            {"q": query, "filter": ",".join(filters), "limit": limit},
        )
        return parse_active(payload)

    async def search_sold(
        self, query: str, *, min_price_cents: int | None = None, limit: int = 100
    ) -> list[EbayItem]:
        if not self.insights_enabled:
            return []
        filters = ["conditionIds:{1000|1500}"]
        if min_price_cents:
            filters += [f"price:[{min_price_cents / 100:.2f}..]", "priceCurrency:USD"]
        try:
            payload = await self._get(
                "/buy/marketplace_insights/v1_beta/item_sales/search",
                INSIGHTS_SCOPE,
                {"q": query, "filter": ",".join(filters), "limit": limit},
            )
        except (InsightsUnavailable, EbayError) as exc:
            if isinstance(exc, InsightsUnavailable) or exc.status in (400, 403):
                logger.warning(
                    "eBay sold data unavailable (%s); using active listings only. Apply for "
                    "Marketplace Insights access to enable sold prices.",
                    exc,
                )
                self.insights_enabled = False
                return []
            raise
        return parse_sold(payload)
