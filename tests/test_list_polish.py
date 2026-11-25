"""How lists read: product names over store ids, merged flapping, price lows, watch order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tracker.bot.embeds import category_name, item_name
from tracker.bot.screens import by_status, collapse, event_line, price_note
from tracker.domain import EventKind, StockStatus
from tracker.outbox import RadarEvent

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def event(id: int, kind: EventKind, listing_id: int = 1, **extra: object) -> RadarEvent:
    base = {
        "id": id,
        "kind": kind,
        "occurred_at": NOW - timedelta(minutes=id),
        "old_value": None,
        "new_value": None,
        "listing_id": listing_id,
        "source": "unifi",
        "region": "us",
        "url": "https://store.ui.com/us/en/products/x",
        "sku": "UVC-AI-PTZ",
        "product_name": "AI PTZ",
        "category": "cameras",
        "pack_size": 1,
        "status": StockStatus.IN_STOCK,
        "price_cents": 49900,
        "currency": "USD",
        "restock_eta": None,
        "is_early_access": False,
    }
    return RadarEvent(**{**base, **extra})  # type: ignore[arg-type]


def test_store_ids_show_the_product_name_and_real_skus_stay() -> None:
    assert item_name("UVC-G5-Pro", "G5 Pro") == "UVC-G5-Pro"
    assert item_name("AMZN-B0G882HQVC", "Ubiquiti Flex Mini switch") == "Ubiquiti Flex Mini switch"
    assert item_name("BBY-6614151", "A" * 60) == "A" * 39 + "…"
    assert item_name("BBY-6614151", None) == "BBY-6614151"
    assert item_name(None, None) == "item"


def test_categories_read_as_words() -> None:
    assert category_name("cloud-gateways-compact") == "Cloud gateways compact"
    assert category_name(None) == "Uncategorized"


def test_flapping_listing_becomes_one_line_with_a_count() -> None:
    events = [
        event(1, EventKind.BACK_IN_STOCK),
        event(2, EventKind.SOLD_OUT),
        event(3, EventKind.BACK_IN_STOCK),
        event(4, EventKind.BACK_IN_STOCK, listing_id=2, sku="UACC-Cable"),
        event(5, EventKind.RESTOCK_ETA, old_value=None, new_value="2026-12-01"),
        event(6, EventKind.RESTOCK_ETA, old_value=None, new_value="2026-12-01"),
        event(7, EventKind.PRICE_CHANGE, old_value="$499", new_value="$449"),
    ]
    grouped = collapse(events)
    assert [(e.id, n) for e, n in grouped] == [(1, 3), (4, 1), (5, 2), (7, 1)]
    line = event_line(*grouped[0])
    assert "Back in stock" in line
    assert line.endswith("· 3 changes")
    assert "changes" not in event_line(*grouped[1])


def test_price_drop_reads_as_a_drop() -> None:
    line = event_line(event(1, EventKind.PRICE_CHANGE, old_value="$499.00", new_value="$449.00"))
    assert "Price drop" in line


def test_price_note_says_when_it_is_the_low() -> None:
    listing = SimpleNamespace(price_cents=44900, currency="USD")
    assert price_note(listing, (44900, 49900)) == "📉 Lowest price in 90 days"  # type: ignore[arg-type]
    assert price_note(listing, (39900, 49900)) == "Was $399.00 at its lowest (90 days)"  # type: ignore[arg-type]
    assert price_note(listing, (44900, 44900)) is None  # type: ignore[arg-type]
    assert price_note(listing, None) is None  # type: ignore[arg-type]


def test_watches_list_in_stock_first() -> None:
    def row(sku: str, status: StockStatus) -> SimpleNamespace:
        return SimpleNamespace(
            listing=SimpleNamespace(status=status.value),
            variant=SimpleNamespace(sku=sku),
            product=SimpleNamespace(name=sku),
        )

    rows = [
        row("A", StockStatus.UNKNOWN),
        row("B", StockStatus.OUT_OF_STOCK),
        row("C", StockStatus.IN_STOCK),
        row("D", StockStatus.COMING_SOON),
        row("E", StockStatus.IN_STOCK),
    ]
    assert [r.variant.sku for r in by_status(rows)] == ["C", "E", "D", "B", "A"]
