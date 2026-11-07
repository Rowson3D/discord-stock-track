"""Core domain types shared by sources, storage, and engines.

Money is always integer cents. Datetimes are always timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class StockStatus(StrEnum):
    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    COMING_SOON = "coming_soon"
    UNKNOWN = "unknown"

    @property
    def is_available(self) -> bool:
        return self in (StockStatus.IN_STOCK, StockStatus.LOW_STOCK)

    @property
    def is_known(self) -> bool:
        return self is not StockStatus.UNKNOWN


class SourceKey(StrEnum):
    UNIFI = "unifi"
    AMAZON = "amazon"
    BESTBUY = "bestbuy"
    BHPHOTO = "bhphoto"
    NEWEGG = "newegg"


class Priority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class EventKind(StrEnum):
    NEW_LISTING = "new_listing"
    BACK_IN_STOCK = "back_in_stock"
    SOLD_OUT = "sold_out"
    STATUS_CHANGE = "status_change"
    PRICE_CHANGE = "price_change"
    RESTOCK_ETA = "restock_eta"
    EARLY_ACCESS = "early_access"
    DELISTED = "delisted"
    RELISTED = "relisted"


@dataclass(frozen=True, slots=True)
class ListingSnapshot:
    """What a retail source reports about one purchasable variant at one moment.

    Product and variant fields are denormalized so a snapshot is self-contained;
    ingestion resolves them into normalized rows.
    """

    source: SourceKey
    region: str
    external_id: str  # Source-specific id of this listing, e.g. regional SKU "U7-Pro-5-US".
    url: str

    product_key: str  # Canonical product identity, e.g. "ubiquiti:u7-pro".
    product_name: str
    brand: str
    category: str | None

    sku: str  # Canonical cross-region SKU, e.g. "U7-Pro-5".
    variant_title: str | None
    pack_size: int

    status: StockStatus
    raw_status: str | None
    price_cents: int | None
    regular_price_cents: int | None  # Pre-discount price when the item is on sale.
    currency: str | None
    is_early_access: bool | None  # None = this view of the source doesn't say.
    sold_out_at: datetime | None
    restock_eta: date | None
    quantity: int | None = None


def format_cents(cents: int | None, currency: str | None = "USD") -> str:
    if cents is None:
        return "—"
    symbol = {"USD": "$", "CAD": "CA$", "GBP": "£", "EUR": "€"}.get(currency or "USD", "")
    return f"{symbol}{cents / 100:,.2f}" + ("" if symbol else f" {currency}")
