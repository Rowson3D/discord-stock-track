"""Pure parsing of the Ubiquiti store's embedded Next.js data (`__NEXT_DATA__`).

The store renders product and category pages server-side and embeds the full GraphQL
payload as JSON, so no browser is needed. Pages of interest:

* product page  `/{region}/{lang}/products/{slug}`  → `pageProps.collection.products[]`
  (the current product is `pageProps.currentProductId`; only these carry `isEarlyAccess`)
* category page `/{region}/{lang}/category/{slug}`  → `pageProps.subCategories[].products[]`
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from tracker.domain import ListingSnapshot, SourceKey, StockStatus

logger = logging.getLogger(__name__)

BRAND = "Ubiquiti"

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL
)
_CATEGORY_LINK_RE = re.compile(
    r"/(?P<region>[a-z]{2,5})/(?P<lang>[a-z]{2})/category/(?P<slug>[a-z0-9-]+)"
)
_PACK_RE = re.compile(r"(\d+)\s*-?\s*pack", re.IGNORECASE)

STATUS_MAP: dict[str, StockStatus] = {
    "available": StockStatus.IN_STOCK,
    "limitedstock": StockStatus.LOW_STOCK,
    "lowstock": StockStatus.LOW_STOCK,
    "soldout": StockStatus.OUT_OF_STOCK,
    "outofstock": StockStatus.OUT_OF_STOCK,
    "comingsoon": StockStatus.COMING_SOON,
    "preorder": StockStatus.COMING_SOON,
}


class UnifiParseError(ValueError):
    """The page didn't contain the data shape we expect. Treat as a schema canary."""


@dataclass(slots=True)
class CategoryPage:
    region: str
    slug: str
    subcategory_ids: list[str] = field(default_factory=list)
    snapshots: list[ListingSnapshot] = field(default_factory=list)


def extract_next_data(html: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise UnifiParseError("__NEXT_DATA__ script not found")
    try:
        data = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise UnifiParseError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "props" not in data:
        raise UnifiParseError("__NEXT_DATA__ has no props")
    return data


def map_status(raw: str | None) -> StockStatus:
    if not raw:
        return StockStatus.UNKNOWN
    status = STATUS_MAP.get(re.sub(r"[^a-z]", "", raw.lower()))
    if status is None:
        logger.warning("Unrecognized UniFi stock status %r; treating as unknown", raw)
        return StockStatus.UNKNOWN
    return status


def pack_size_from_title(title: str | None) -> int:
    if title:
        match = _PACK_RE.search(title)
        if match:
            return max(1, int(match.group(1)))
    return 1


def strip_region_suffix(sku: str, region: str) -> str:
    suffix = f"-{region}".lower()
    return sku[: -len(suffix)] if sku.lower().endswith(suffix) else sku


def product_url(base_url: str, region: str, language: str, slug: str) -> str:
    return f"{base_url}/{region}/{language}/products/{slug}"


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _money(value: Any) -> tuple[int | None, str | None]:
    """(cents, currency). Unpriced items (e.g. coming soon) report 0, which we treat as None."""
    if isinstance(value, dict) and isinstance(value.get("amount"), int | float):
        amount = int(value["amount"])
        return (amount if amount > 0 else None), value.get("currency")
    return None, None


def _route(data: dict[str, Any]) -> tuple[str, str]:
    query = data.get("query") or {}
    return str(query.get("store") or "us"), str(query.get("language") or "en")


def snapshots_from_product(
    product: dict[str, Any],
    *,
    base_url: str,
    region: str,
    language: str,
    category: str | None,
) -> Iterator[ListingSnapshot]:
    slug = product.get("slug")
    if not slug:
        return

    url = product_url(base_url, region, language, slug)
    name = product.get("title") or product.get("shortTitle") or product.get("name") or slug
    # Option values ("5-Pack", "Black", …) are the only variant labels on category pages.
    option_labels = {
        value["id"]: value.get("title")
        for option in product.get("options") or []
        for value in option.get("values") or []
        if value.get("id")
    }

    for variant in product.get("variants") or []:
        regional_sku = variant.get("sku")
        if not regional_sku or variant.get("isVisibleInStore") is False:
            continue
        price, currency = _money(variant.get("displayPrice"))
        regular, _ = _money(variant.get("displayRegularPrice"))
        labels = [
            label
            for value_id in variant.get("optionValueIds") or []
            if (label := option_labels.get(value_id))
        ]
        title = variant.get("title")
        if title in (None, "Default"):
            title = " / ".join(labels) or None
        early_access = variant.get("isEarlyAccess")

        yield ListingSnapshot(
            source=SourceKey.UNIFI,
            region=region,
            external_id=regional_sku,
            url=url,
            # Slugs are stable across regions; display SKUs carry notes like "(400W)".
            product_key=f"ubiquiti:{slug}",
            product_name=name,
            brand=BRAND,
            category=category,
            sku=strip_region_suffix(regional_sku, region),
            variant_title=title,
            pack_size=pack_size_from_title(" ".join(filter(None, [title, *labels]))),
            status=map_status(variant.get("status")),
            raw_status=variant.get("status"),
            price_cents=price,
            regular_price_cents=regular if regular and regular != price else None,
            currency=currency,
            is_early_access=early_access if isinstance(early_access, bool) else None,
            sold_out_at=_parse_datetime(variant.get("soldOutAt")),
            restock_eta=_parse_date(variant.get("restockEtaAt")),
        )


def parse_product_page(data: dict[str, Any], *, base_url: str) -> list[ListingSnapshot]:
    """Snapshots for the page's own product only (not related/upsell products)."""
    page_props = data.get("props", {}).get("pageProps") or {}
    products = (page_props.get("collection") or {}).get("products")
    if not isinstance(products, list) or not products:
        raise UnifiParseError("product page has no collection.products")

    current_id = page_props.get("currentProductId")
    current = [p for p in products if p.get("id") == current_id] or products
    category = ((page_props.get("category") or {}).get("currentCategory") or {}).get("id")
    region, language = _route(data)

    return [
        snapshot
        for product in current
        for snapshot in snapshots_from_product(
            product, base_url=base_url, region=region, language=language, category=category
        )
    ]


def is_category_page(data: dict[str, Any]) -> bool:
    """False when the store redirected (e.g. `whats-new` renders the home page)."""
    return "/category/" in str(data.get("page") or "")


def parse_category_page(data: dict[str, Any], *, base_url: str) -> CategoryPage:
    page_props = data.get("props", {}).get("pageProps") or {}
    subcategories = page_props.get("subCategories")
    if not isinstance(subcategories, list):
        raise UnifiParseError("category page has no subCategories")

    region, language = _route(data)
    page = CategoryPage(region=region, slug=str((data.get("query") or {}).get("category") or ""))
    for sub in subcategories:
        sub_id = sub.get("id")
        if sub_id:
            page.subcategory_ids.append(sub_id)
        for product in sub.get("products") or []:
            page.snapshots.extend(
                snapshots_from_product(
                    product,
                    base_url=base_url,
                    region=region,
                    language=language,
                    category=product.get("subcategoryId") or sub_id,
                )
            )
    return page


def find_category_slugs(html: str, region: str | None = None) -> list[str]:
    """Category slugs linked from a page, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _CATEGORY_LINK_RE.finditer(html):
        if region is None or match.group("region") == region:
            seen.setdefault(match.group("slug"), None)
    return list(seen)


def dedupe(snapshots: Iterable[ListingSnapshot]) -> list[ListingSnapshot]:
    """Keep the first snapshot per listing identity (products appear in several categories)."""
    unique: dict[tuple[str, str, str], ListingSnapshot] = {}
    for snapshot in snapshots:
        unique.setdefault((snapshot.source, snapshot.region, snapshot.external_id), snapshot)
    return list(unique.values())
