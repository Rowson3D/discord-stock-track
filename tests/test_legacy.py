from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_snapshot
from tracker.db.models import Listing, Watch
from tracker.domain import ListingSnapshot, SourceKey, StockStatus
from tracker.http import FetchError
from tracker.legacy import import_legacy, legacy_sku, parse_legacy_price


class FakeUnifi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_product(self, slug: str, region: str = "us") -> list[ListingSnapshot]:
        self.calls.append((slug, region))
        if slug == "gone":
            raise FetchError("https://store.ui.com/us/en/products/gone", "HTTP 404", 404)
        return [
            make_snapshot(external_id=f"{slug.upper()}-US", sku=slug.upper()),
            make_snapshot(external_id=f"{slug.upper()}-5-US", sku=f"{slug.upper()}-5", pack_size=5),
        ]


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def legacy_files(tmp_path: Path) -> tuple[Path, Path]:
    watchlist = _write(
        tmp_path / "watchlist.json",
        [
            {
                "url": "https://store.ui.com/us/en/products/u7-pro",
                "name": "U7 Pro",
                "site": "ui.com",
                "priority": "high",
                "watchers": ["111", "222"],
                "last_status": "in_stock",
            },
            {
                "url": "https://www.amazon.com/dp/B0ABCDEFGH",
                "name": "https://www.amazon.com/dp/B0ABCDEFGH",
                "site": "amazon.com",
                "last_status": "out_of_stock",
                "last_price": "$1,299.99",
                "watchers": ["111"],
                "checkout": {"enabled": True},
            },
            {"url": "https://store.ui.com/us/en/products/gone", "site": "ui.com"},
            {"url": "https://example.com/thing", "site": "example.com"},
        ],
    )
    subscribers = _write(tmp_path / "subscribers.json", {"user_ids": ["999", "111"]})
    return watchlist, subscribers


async def test_import_legacy(session: AsyncSession, legacy_files: tuple[Path, Path]):
    watchlist, subscribers = legacy_files
    unifi = FakeUnifi()
    report = await import_legacy(
        session, watchlist_path=watchlist, subscribers_path=subscribers, unifi=unifi
    )

    assert unifi.calls == [("u7-pro", "us"), ("gone", "us")]
    assert report.entries == 4
    assert report.listings_created == 3  # two U7 Pro variants + one Amazon URL
    assert report.skipped_checkout_configs == 1
    assert len(report.unresolved) == 2

    listings = {item.external_id: item for item in (await session.scalars(select(Listing))).all()}
    assert listings["U7-PRO-US"].priority == "high"
    amazon = listings["https://www.amazon.com/dp/B0ABCDEFGH"]
    assert amazon.source == SourceKey.AMAZON
    assert amazon.status == StockStatus.OUT_OF_STOCK
    assert amazon.price_cents == 129999

    watches = (await session.scalars(select(Watch))).all()
    per_listing = sorted((w.discord_user_id, w.listing_id is None) for w in watches)
    # 2 watchers x 2 U7 variants + 1 Amazon watcher + 2 global subscribers
    assert len(watches) == 7
    assert per_listing.count(("999", True)) == 1
    assert per_listing.count(("111", True)) == 1

    again = await import_legacy(
        session, watchlist_path=watchlist, subscribers_path=subscribers, unifi=unifi
    )
    assert again.listings_created == 0
    assert again.watches_created == 0


async def test_import_without_resolver_skips_unifi(
    session: AsyncSession, legacy_files: tuple[Path, Path]
):
    watchlist, _ = legacy_files
    report = await import_legacy(
        session, watchlist_path=watchlist, subscribers_path=None, unifi=None
    )
    assert report.listings_created == 1
    assert any("UniFi resolution unavailable" in item for item in report.unresolved)


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        ("$189.00", 18900),
        ("$1,299.99", 129999),
        ("$5", 500),
        ("$5.5", 550),
        (None, None),
        ("", None),
    ],
)
def test_parse_legacy_price(value: str | None, cents: int | None):
    assert parse_legacy_price(value) == cents


def test_legacy_sku():
    amazon = {"url": "https://www.amazon.com/Some-Thing/dp/B0ABCDEFGH/ref=x"}
    bestbuy = {"url": "https://www.bestbuy.com/site/rtx/6521430.p?skuId=6521430"}
    assert legacy_sku(amazon, SourceKey.AMAZON) == "AMZN-B0ABCDEFGH"
    assert legacy_sku(bestbuy, SourceKey.BESTBUY) == "BBY-6521430"
    assert legacy_sku({"url": "x", "sku": "RTX-4090-FE"}, SourceKey.BESTBUY) == "RTX-4090-FE"
    assert legacy_sku({"url": "https://newegg.com/p/N82E1"}, SourceKey.NEWEGG) == "NEWEGG-N82E1"
