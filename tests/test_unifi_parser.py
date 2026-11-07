"""Parser tests run against trimmed real store pages captured 2026-11-07."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from tracker.domain import SourceKey, StockStatus
from tracker.sources.unifi.parser import (
    UnifiParseError,
    dedupe,
    extract_next_data,
    find_category_slugs,
    is_category_page,
    map_status,
    pack_size_from_title,
    page_region,
    parse_category_page,
    parse_home_page,
    parse_product_page,
    strip_region_suffix,
)

BASE = "https://store.ui.com"


def test_product_page_yields_only_current_product_variants(fixture_text: Callable[[str], str]):
    data = extract_next_data(fixture_text("unifi/product_u7-pro.html"))
    snapshots = parse_product_page(data, base_url=BASE)

    assert [s.external_id for s in snapshots] == ["U7-Pro-US", "U7-Pro-5-US"]
    single, five_pack = snapshots
    assert single.source is SourceKey.UNIFI
    assert single.sku == "U7-Pro"
    assert single.product_key == "ubiquiti:u7-pro"
    assert single.product_name == "Access Point U7 Pro"
    assert single.url == "https://store.ui.com/us/en/products/u7-pro"
    assert single.category == "wifi-flagship"
    assert single.status is StockStatus.IN_STOCK
    assert single.price_cents == 18900
    assert single.currency == "USD"
    assert single.is_early_access is False
    assert single.pack_size == 1
    assert five_pack.sku == "U7-Pro-5"
    assert five_pack.variant_title == "5-Pack"
    assert five_pack.pack_size == 5
    assert five_pack.price_cents == 89900


def test_category_page_parses_sold_out_details(fixture_text: Callable[[str], str]):
    data = extract_next_data(fixture_text("unifi/category_all-wifi.html"))
    assert is_category_page(data)
    page = parse_category_page(data, base_url=BASE)

    assert page.region == "us"
    assert page.slug == "all-wifi"
    assert page.subcategory_ids == ["wifi-flagship", "wifi-bridging", "wifi-whole-home-wifi"]
    by_id = {s.external_id: s for s in page.snapshots}

    iot = by_id["UDB-IoT-US"]
    assert iot.status is StockStatus.OUT_OF_STOCK
    assert iot.raw_status == "SoldOut"
    assert iot.sold_out_at == datetime(2026, 11, 7, 10, 5, 34, 520000, tzinfo=UTC)
    assert iot.restock_eta == date(2026, 12, 8)
    assert iot.category == "wifi-bridging"
    # Category pages don't say whether something is Early Access.
    assert iot.is_early_access is None


def test_category_page_derives_pack_size_from_option_labels(fixture_text: Callable[[str], str]):
    page = parse_category_page(
        extract_next_data(fixture_text("unifi/category_all-wifi.html")), base_url=BASE
    )
    by_id = {s.external_id: s for s in page.snapshots}

    # The SKU doesn't reveal it, but UBB-XG-US is the 2-pack.
    assert by_id["UBB-XG-US"].pack_size == 2
    assert by_id["UBB-XG-US"].variant_title == "2-Pack"
    assert by_id["UBB-XG-Single-US"].pack_size == 1
    assert by_id["UX7-3-US"].pack_size == 3
    assert by_id["UX7-3-US"].variant_title == "US Version / 3-Pack"


def test_hidden_variants_are_skipped(fixture_text: Callable[[str], str]):
    data = extract_next_data(fixture_text("unifi/product_u7-pro.html"))
    data["props"]["pageProps"]["collection"]["products"][0]["variants"][1]["isVisibleInStore"] = (
        False
    )
    assert [s.external_id for s in parse_product_page(data, base_url=BASE)] == ["U7-Pro-US"]


def test_zero_price_is_treated_as_unpriced(fixture_text: Callable[[str], str]):
    data = extract_next_data(fixture_text("unifi/product_u7-pro.html"))
    data["props"]["pageProps"]["collection"]["products"][0]["variants"][0]["displayPrice"][
        "amount"
    ] = 0
    assert parse_product_page(data, base_url=BASE)[0].price_cents is None


def test_home_page_is_not_a_category_page(fixture_text: Callable[[str], str]):
    assert not is_category_page(extract_next_data(fixture_text("unifi/home.html")))


def test_find_category_slugs(fixture_text: Callable[[str], str]):
    slugs = find_category_slugs(fixture_text("unifi/home.html"), "us")
    assert "all-wifi" in slugs
    assert "all-switching" in slugs
    assert len(slugs) == len(set(slugs))
    assert find_category_slugs(fixture_text("unifi/home.html"), "ca") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Available", StockStatus.IN_STOCK),
        ("SoldOut", StockStatus.OUT_OF_STOCK),
        ("ComingSoon", StockStatus.COMING_SOON),
        ("Unavailable", StockStatus.OUT_OF_STOCK),
        ("RegionNotAvailable", StockStatus.OUT_OF_STOCK),
        ("LIMITED_STOCK", StockStatus.LOW_STOCK),
        ("SomethingNew", StockStatus.UNKNOWN),
        (None, StockStatus.UNKNOWN),
    ],
)
def test_map_status(raw: str | None, expected: StockStatus):
    assert map_status(raw) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [("5-Pack", 5), ("US Version / 3-Pack", 3), ("Single Unit", 1), ("Black", 1), (None, 1)],
)
def test_pack_size_from_title(title: str | None, expected: int):
    assert pack_size_from_title(title) == expected


def test_strip_region_suffix():
    assert strip_region_suffix("U7-Pro-5-US", "us") == "U7-Pro-5"
    assert strip_region_suffix("USW-Pro-XG-10-PoE", "us") == "USW-Pro-XG-10-PoE"
    assert strip_region_suffix("UVC-G4 Doorbell Pro-US", "us") == "UVC-G4 Doorbell Pro"


def test_dedupe_keeps_first_occurrence(fixture_text: Callable[[str], str]):
    page = parse_category_page(
        extract_next_data(fixture_text("unifi/category_all-wifi.html")), base_url=BASE
    )
    doubled = page.snapshots + page.snapshots
    assert dedupe(doubled) == page.snapshots


@pytest.mark.parametrize(
    "html",
    [
        "<html>no data</html>",
        '<script id="__NEXT_DATA__" type="application/json">{not json</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"page": "/"}</script>',
    ],
)
def test_extract_next_data_errors(html: str):
    with pytest.raises(UnifiParseError):
        extract_next_data(html)


def test_category_parse_error_on_wrong_shape():
    with pytest.raises(UnifiParseError):
        parse_category_page({"props": {"pageProps": {}}}, base_url=BASE)
    with pytest.raises(UnifiParseError):
        parse_product_page({"props": {"pageProps": {}}}, base_url=BASE)


def test_home_page_feeds(fixture_text: Callable[[str], str]):
    home = parse_home_page(fixture_text("unifi/home.html"))
    assert home.region == "us"
    assert "all-wifi" in home.category_slugs
    assert len(home.freshly_stocked) == 4
    first = home.freshly_stocked[0]
    assert first.regional_sku == "USW-Pro-48-POE"
    assert first.product_slug == "usw-pro-48-poe"
    assert first.stocked_at == datetime(2026, 11, 6, 16, 7, 13, 766000, tzinfo=UTC)
    assert home.whats_new_slugs[0] == "usw-lr-switch-8-poe"
    assert len(home.whats_new_slugs) == len(set(home.whats_new_slugs))


def test_canadian_store_sells_us_skus_in_cad(fixture_text: Callable[[str], str]):
    data = extract_next_data(fixture_text("unifi/product_u7-pro_ca.html"))
    assert page_region(data) == "ca"
    single = parse_product_page(data, base_url="https://ca.store.ui.com")[0]
    assert single.region == "ca"
    assert single.external_id == "U7-Pro-US"
    assert single.sku == "U7-Pro"  # same canonical variant as the US listing
    assert single.currency == "CAD"
    assert single.url == "https://ca.store.ui.com/ca/en/products/u7-pro"


@pytest.mark.parametrize(
    ("sku", "region", "expected"),
    [
        ("U7-Pro-US", "ca", "U7-Pro"),
        ("U7-Pro", "uk", "U7-Pro"),
        ("U7-Pro-EU", "eu", "U7-Pro"),
        ("U7-Pro-JP", "jp", "U7-Pro"),
    ],
)
def test_strip_region_suffix_per_region(sku: str, region: str, expected: str):
    assert strip_region_suffix(sku, region) == expected
