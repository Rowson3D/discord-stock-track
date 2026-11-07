"""URL-based retailers (Amazon, Best Buy, B&H, Newegg).

Parsing order: schema.org JSON-LD `Product`/`Offer` data first (structured, stable),
then per-site fallbacks ported from the original bot. Anti-bot pages (captcha, robot
checks) raise `BlockedError` so they're never mistaken for a stock change.

Reality check (2026-11-07): from a residential connection all four sites block plain
HTTP clients (403 / captcha / stream reset). Results from a server IP may differ;
listings that stay blocked back off automatically.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from tracker.domain import ListingSnapshot, SourceKey, StockStatus
from tracker.http import HttpClient

SOURCE_DOMAINS: dict[SourceKey, tuple[str, ...]] = {
    SourceKey.UNIFI: ("store.ui.com",),
    SourceKey.AMAZON: ("amazon.com",),
    SourceKey.BESTBUY: ("bestbuy.com",),
    SourceKey.BHPHOTO: ("bhphotovideo.com",),
    SourceKey.NEWEGG: ("newegg.com",),
}
WEB_SOURCES = (SourceKey.AMAZON, SourceKey.BESTBUY, SourceKey.BHPHOTO, SourceKey.NEWEGG)
SOURCE_NAMES = {
    SourceKey.UNIFI: "Ubiquiti Store",
    SourceKey.AMAZON: "Amazon",
    SourceKey.BESTBUY: "Best Buy",
    SourceKey.BHPHOTO: "B&H Photo",
    SourceKey.NEWEGG: "Newegg",
}

_ASIN_RE = re.compile(r"/(?:dp|gp/product)/(?P<asin>[A-Z0-9]{10})")
_BH_ID_RE = re.compile(r"/c/product/(?P<id>\d+)-")
_NEWEGG_ID_RE = re.compile(r"/p/(?P<id>[A-Z0-9-]+)", re.IGNORECASE)
_MONEY_RE = re.compile(r"(\d[\d,]*)(?:\.(\d{1,2}))?")
_BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "robot check",
    "access denied",
    "pardon our interruption",
    "attention required",
    "cf-chl",
    "verify you are a human",
    "challenges.cloudflare.com",
    "<title>just a moment...</title>",
)

AVAILABILITY: dict[str, StockStatus] = {
    "instock": StockStatus.IN_STOCK,
    "onlineonly": StockStatus.IN_STOCK,
    "instoreonly": StockStatus.IN_STOCK,
    "limitedavailability": StockStatus.LOW_STOCK,
    "outofstock": StockStatus.OUT_OF_STOCK,
    "soldout": StockStatus.OUT_OF_STOCK,
    "discontinued": StockStatus.OUT_OF_STOCK,
    "backorder": StockStatus.OUT_OF_STOCK,
    "preorder": StockStatus.COMING_SOON,
    "presale": StockStatus.COMING_SOON,
}


class BlockedError(Exception):
    """The retailer served an anti-bot page instead of the product."""


def _host(url: str) -> str:
    host = urlsplit(url.strip()).netloc.lower().split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def detect_source(url: str) -> SourceKey | None:
    """Exact domain or subdomain match (no substring tricks like `evil-amazon.com.example`)."""
    host = _host(url)
    for source, domains in SOURCE_DOMAINS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return source
    return None


def parse_money(value: Any) -> int | None:
    """'$1,299.99' / '1299.99' / 1299.99 → cents."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return round(float(value) * 100) if value > 0 else None
    match = _MONEY_RE.search(str(value))
    if not match:
        return None
    cents = int(match.group(1).replace(",", "")) * 100 + int((match.group(2) or "0").ljust(2, "0"))
    return cents or None


def listing_sku(url: str, source: SourceKey, explicit: str | None = None) -> str:
    """Stable per-retailer id used as the variant SKU for URL listings."""
    if explicit:
        return explicit
    parts = urlsplit(url)
    if source is SourceKey.AMAZON and (match := _ASIN_RE.search(parts.path)):
        return f"AMZN-{match.group('asin')}"
    if source is SourceKey.BESTBUY and (sku_ids := parse_qs(parts.query).get("skuId")):
        return f"BBY-{sku_ids[0]}"
    if source is SourceKey.BHPHOTO and (match := _BH_ID_RE.search(parts.path)):
        return f"BH-{match.group('id')}"
    if source is SourceKey.NEWEGG and (match := _NEWEGG_ID_RE.search(parts.path)):
        return f"NEWEGG-{match.group('id').upper()}"
    slug = parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.netloc
    return f"{source.value.upper()}-{slug}"[:100]


# ── JSON-LD ──────────────────────────────────────────────────────────────


def _walk_jsonld(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def _is_type(node: dict[str, Any], name: str) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).lower() == name.lower() for k in kinds)


def jsonld_products(soup: BeautifulSoup) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        products.extend(node for node in _walk_jsonld(data) if _is_type(node, "Product"))
    return products


def _offers(product: dict[str, Any]) -> list[dict[str, Any]]:
    offers = product.get("offers")
    items = offers if isinstance(offers, list) else [offers]
    flat: list[dict[str, Any]] = []
    for offer in items:
        if isinstance(offer, dict):
            nested = offer.get("offers")
            if _is_type(offer, "AggregateOffer") and nested:
                flat.extend(o for o in (nested if isinstance(nested, list) else [nested]) if o)
            else:
                flat.append(offer)
    return flat


def map_availability(value: Any) -> StockStatus:
    if not value:
        return StockStatus.UNKNOWN
    key = str(value).rstrip("/").rsplit("/", 1)[-1].lower()
    return AVAILABILITY.get(key, StockStatus.UNKNOWN)


def _from_jsonld(soup: BeautifulSoup) -> dict[str, Any]:
    for product in jsonld_products(soup):
        offers = _offers(product)
        statuses = [map_availability(o.get("availability")) for o in offers]
        known = [s for s in statuses if s.is_known]
        # Any purchasable offer wins over sold-out marketplace offers.
        status = next((s for s in known if s.is_available), known[0] if known else None)
        priced = [o for o in offers if parse_money(o.get("price") or o.get("lowPrice"))]
        offer = priced[0] if priced else {}
        return {
            "name": product.get("name"),
            "status": status or StockStatus.UNKNOWN,
            "price_cents": parse_money(offer.get("price") or offer.get("lowPrice")),
            "currency": offer.get("priceCurrency"),
            "brand": (product.get("brand") or {}).get("name")
            if isinstance(product.get("brand"), dict)
            else product.get("brand"),
        }
    return {}


# ── Per-site fallbacks (ported from the original scrapers) ──────────────


def _text(soup: BeautifulSoup, selector: str) -> str | None:
    element = soup.select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def _classify(text: str, in_stock: tuple[str, ...], out: tuple[str, ...], low: tuple[str, ...]):
    text = re.sub(r"\s+", " ", text.lower())
    if any(p in text for p in low):
        return StockStatus.LOW_STOCK
    if any(p in text for p in out):
        return StockStatus.OUT_OF_STOCK
    if any(p in text for p in in_stock):
        return StockStatus.IN_STOCK
    return StockStatus.UNKNOWN


def _fallback(soup: BeautifulSoup, source: SourceKey) -> dict[str, Any]:
    if source is SourceKey.AMAZON:
        availability = _text(soup, "#availability") or ""
        status = _classify(
            availability,
            ("in stock",),
            ("currently unavailable", "out of stock", "unavailable"),
            ("only ", "left in stock"),
        )
        if not status.is_known and soup.select_one("#add-to-cart-button"):
            status = StockStatus.IN_STOCK
        return {
            "name": _text(soup, "#productTitle"),
            "status": status,
            "price_cents": parse_money(_text(soup, ".a-price .a-offscreen")),
        }
    # Best Buy / B&H / Newegg: scope text to the buy box when we can find it.
    buy_box = (
        soup.select_one(".fulfillment-add-to-cart-button, [data-selenium='addToCartSection']")
        or soup.select_one("#ProductBuy, .product-buy-box")
        or soup
    )
    return {
        "name": _text(soup, "h1"),
        "status": _classify(
            buy_box.get_text(" "),
            ("add to cart",),
            ("sold out", "out of stock", "back-order", "backordered", "coming soon"),
            ("limited quantity", "only a few left", "limited supply"),
        ),
        "price_cents": parse_money(
            _text(
                soup,
                ".priceView-customer-price span, [data-selenium='pricingPrice'], .price-current",
            )
        ),
    }


def looks_blocked(html: str) -> bool:
    lowered = html[:50_000].lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def parse_retail_page(html: str, url: str, source: SourceKey) -> ListingSnapshot:
    soup = BeautifulSoup(html, "lxml")
    data = _from_jsonld(soup)
    if not data.get("status", StockStatus.UNKNOWN).is_known:
        fallback = _fallback(soup, source)
        data = {
            "name": data.get("name") or fallback["name"],
            "status": fallback["status"],
            "price_cents": data.get("price_cents") or fallback["price_cents"],
            "currency": data.get("currency"),
            "brand": data.get("brand"),
        }

    status: StockStatus = data.get("status") or StockStatus.UNKNOWN
    # Block pages often have an <h1> ("Are you a human?"), so a name proves nothing here.
    if not status.is_known and looks_blocked(html):
        raise BlockedError(f"{SOURCE_NAMES[source]} served an anti-bot page")
    return url_snapshot(
        url,
        source,
        name=data.get("name"),
        status=status,
        price_cents=data.get("price_cents"),
        currency=data.get("currency") or "USD",
        brand=data.get("brand"),
    )


def url_snapshot(
    url: str,
    source: SourceKey,
    *,
    name: str | None = None,
    status: StockStatus = StockStatus.UNKNOWN,
    price_cents: int | None = None,
    currency: str | None = "USD",
    brand: str | None = None,
    sku: str | None = None,
    category: str | None = None,
    quantity: int | None = None,
) -> ListingSnapshot:
    canonical = listing_sku(url, source, sku)
    return ListingSnapshot(
        source=source,
        region="us",
        external_id=url,
        url=url,
        product_key=f"{source.value}:{canonical.lower()}",
        product_name=(name or canonical)[:300],
        brand=str(brand or SOURCE_NAMES.get(source, source.value))[:100],
        category=category,
        sku=canonical,
        variant_title=None,
        pack_size=1,
        status=status,
        raw_status=status.value,
        price_cents=price_cents,
        regular_price_cents=None,
        currency=currency,
        is_early_access=None,
        sold_out_at=None,
        restock_eta=None,
        quantity=quantity,
    )


async def fetch_listing(http: HttpClient, url: str, source: SourceKey) -> ListingSnapshot:
    """Raises FetchError (HTTP/transport) or BlockedError (anti-bot page)."""
    return parse_retail_page(await http.get_text(url), url, source)
