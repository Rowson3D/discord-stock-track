"""One-time import of the original bot's `watchlist.json` and `subscribers.json`.

* UniFi URLs are resolved through the store parser into real per-variant listings.
* Other retailers become URL-keyed listings (their scrapers are ported in Phase 2).
* Per-entry `watchers` become watches on the imported listings; global subscribers
  become watches with no listing (alerts for everything).
* Importing is idempotent: running it twice creates nothing new.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import Listing, Watch
from tracker.domain import ListingSnapshot, Priority, SourceKey, StockStatus
from tracker.http import FetchError
from tracker.ingest import Ingestor
from tracker.sources.unifi.parser import UnifiParseError

logger = logging.getLogger(__name__)

LEGACY_SITES: dict[str, SourceKey] = {
    "ui.com": SourceKey.UNIFI,
    "amazon.com": SourceKey.AMAZON,
    "bestbuy.com": SourceKey.BESTBUY,
    "bhphotovideo.com": SourceKey.BHPHOTO,
    "newegg.com": SourceKey.NEWEGG,
}
_UNIFI_PATH_RE = re.compile(r"^/(?P<region>[a-z]{2,5})/[a-z]{2}/products/(?P<slug>[A-Za-z0-9-]+)")
_ASIN_RE = re.compile(r"/(?:dp|gp/product)/(?P<asin>[A-Z0-9]{10})")
_PRICE_RE = re.compile(r"(\d[\d,]*)(?:\.(\d{1,2}))?")


class ProductFetcher(Protocol):
    async def fetch_product(self, slug: str, region: str = "us") -> list[ListingSnapshot]: ...


@dataclass(slots=True)
class ImportReport:
    entries: int = 0
    listings_created: int = 0
    watches_created: int = 0
    unresolved: list[str] = field(default_factory=list)
    skipped_checkout_configs: int = 0


def _read_json(path: Path | None) -> Any:
    """Parsed JSON, or None when no path was given or the file doesn't exist."""
    if path is None or not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def parse_legacy_price(value: Any) -> int | None:
    if value is None:
        return None
    match = _PRICE_RE.search(str(value))
    if not match:
        return None
    whole = int(match.group(1).replace(",", ""))
    fraction = int((match.group(2) or "0").ljust(2, "0"))
    return whole * 100 + fraction


def legacy_status(value: Any) -> StockStatus:
    try:
        return StockStatus(str(value))
    except ValueError:
        return StockStatus.UNKNOWN


def legacy_sku(entry: dict[str, Any], source: SourceKey) -> str:
    """Best-effort stable SKU for a non-UniFi legacy entry."""
    if entry.get("sku"):
        return str(entry["sku"])
    url = str(entry["url"])
    parts = urlsplit(url)
    if source is SourceKey.AMAZON and (match := _ASIN_RE.search(parts.path)):
        return f"AMZN-{match.group('asin')}"
    if source is SourceKey.BESTBUY and (sku_ids := parse_qs(parts.query).get("skuId")):
        return f"BBY-{sku_ids[0]}"
    slug = parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.netloc
    return f"{source.value.upper()}-{slug}"[:100]


def _snapshot_for_url_entry(entry: dict[str, Any], source: SourceKey) -> ListingSnapshot:
    sku = legacy_sku(entry, source)
    name = entry.get("name") if entry.get("name") != entry["url"] else None
    return ListingSnapshot(
        source=source,
        region="us",
        external_id=str(entry["url"]),
        url=str(entry["url"]),
        product_key=f"{source.value}:{sku.lower()}",
        product_name=name or sku,
        brand=str(entry.get("vendor") or source.value),
        category=entry.get("category"),
        sku=sku,
        variant_title=None,
        pack_size=1,
        status=legacy_status(entry.get("last_status")),
        raw_status=entry.get("last_status"),
        price_cents=parse_legacy_price(entry.get("last_price")),
        regular_price_cents=None,
        currency="USD",
        is_early_access=None,
        sold_out_at=None,
        restock_eta=None,
        quantity=entry.get("last_quantity"),
    )


async def _listing_for(session: AsyncSession, snapshot: ListingSnapshot) -> Listing | None:
    return await session.scalar(
        select(Listing).where(
            Listing.source == snapshot.source.value,
            Listing.region == snapshot.region,
            Listing.external_id == snapshot.external_id,
        )
    )


async def _ensure_watch(session: AsyncSession, user_id: str, listing_id: int | None) -> bool:
    condition = Watch.listing_id.is_(None) if listing_id is None else Watch.listing_id == listing_id
    existing = await session.scalar(
        select(Watch.id).where(Watch.discord_user_id == user_id, condition)
    )
    if existing is not None:
        return False
    session.add(Watch(discord_user_id=user_id, listing_id=listing_id))
    return True


async def import_legacy(
    session: AsyncSession,
    *,
    watchlist_path: Path | None,
    subscribers_path: Path | None,
    unifi: ProductFetcher | None,
) -> ImportReport:
    report = ImportReport()
    ingestor = Ingestor(session, emit_events=False)
    payload = await asyncio.to_thread(_read_json, watchlist_path)
    entries = [e for e in payload or [] if isinstance(e, dict) and e.get("url")]

    for entry in entries:
        report.entries += 1
        url = str(entry["url"]).strip()
        source = LEGACY_SITES.get(str(entry.get("site", "")))
        if source is None:
            report.unresolved.append(f"{url} (unsupported site {entry.get('site')!r})")
            continue
        if entry.get("checkout"):
            report.skipped_checkout_configs += 1

        snapshots: list[ListingSnapshot]
        if source is SourceKey.UNIFI:
            match = _UNIFI_PATH_RE.match(urlsplit(url).path)
            if not match or unifi is None:
                report.unresolved.append(f"{url} (UniFi resolution unavailable)")
                continue
            try:
                snapshots = await unifi.fetch_product(match["slug"], match["region"])
            except (FetchError, UnifiParseError) as exc:
                report.unresolved.append(f"{url} ({exc})")
                continue
            if not snapshots:
                report.unresolved.append(f"{url} (no variants on page)")
                continue
        else:
            snapshots = [_snapshot_for_url_entry(entry, source)]

        ingest = await ingestor.ingest(snapshots)
        report.listings_created += ingest.created_listings

        priority = str(entry.get("priority") or Priority.NORMAL.value)
        watchers = [str(w).strip() for w in entry.get("watchers") or [] if str(w).strip()]
        for snapshot in snapshots:
            listing = await _listing_for(session, snapshot)
            if listing is None:
                continue
            if priority in {p.value for p in Priority}:
                listing.priority = priority
            for user_id in watchers:
                report.watches_created += await _ensure_watch(session, user_id, listing.id)

    payload = await asyncio.to_thread(_read_json, subscribers_path)
    user_ids = payload.get("user_ids", []) if isinstance(payload, dict) else payload
    for user_id in user_ids or []:
        if str(user_id).strip():
            report.watches_created += await _ensure_watch(session, str(user_id).strip(), None)

    await session.flush()
    return report
