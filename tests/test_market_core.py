from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import T0
from tracker.market.matching import (
    VariantMatcher,
    base_sku,
    condition_from_id,
    condition_from_title,
    detect_quantity,
)
from tracker.market.valuation import (
    Comp,
    drop_outliers,
    is_deal,
    opportunity_score,
    percentile,
    value_from_comps,
)
from tracker.settings import MarketSettings

CATALOG = [
    "U7-Pro", "U7-Pro-5", "U7-Pro-Max", "U7-Pro-XG", "U7-Pro-XGS", "U7-Pro-Wall",
    "U7-Pro-Outdoor", "UDM-Pro", "UDM-Pro-Max", "UDM-SE", "UCG-Max", "UCG-Max-NS",
    "UNVR", "UNVR-Pro",
]  # fmt: skip


def matcher(sku: str, pack: int = 1) -> VariantMatcher:
    return VariantMatcher(sku, pack, CATALOG)


@pytest.mark.parametrize(
    "title",
    [
        "Ubiquiti UniFi U7 Pro WiFi 7 Access Point U7-Pro NEW SEALED",
        "Ubiquiti U7-Pro Wi-Fi 7 AP 6GHz - Brand New",
        "UBIQUITI U7PRO ACCESS POINT",
        "Ubiquiti Networks U7-Pro-US WiFi 7 Ceiling AP (includes mount)",
    ],
)
def test_matches_real_u7_pro_titles(title: str):
    result = matcher("U7-Pro").match(title)
    assert result.matched, result.reason
    assert result.score == 1.0


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Ubiquiti UniFi U7 Pro Max WiFi 7 Access Point", "different model"),
        ("Ubiquiti U7-Pro-XGS PoE++ WiFi 7", "different model"),
        ("UniFi U7 Pro Wall Access Point", "different model"),
        ("Ubiquiti U7 Pro - for parts, no power", "parts or not working"),
        ("Ceiling mount bracket for Ubiquiti U7 Pro", "accessory"),
        ("Ubiquiti U7 Pro EMPTY BOX only", "box only"),
        ("Ubiquiti U6 Pro Access Point", "sku not in title"),
    ],
)
def test_rejects_wrong_or_bad_listings(title: str, reason: str):
    result = matcher("U7-Pro").match(title)
    assert not result.matched
    assert reason in result.reason


def test_model_modifier_without_catalog_sibling():
    # "UDM Pro SE" isn't a catalog SKU, but "SE" right after the phrase means another model.
    assert not matcher("UDM-Pro").match("Ubiquiti UDM Pro SE Dream Machine").matched
    assert matcher("UDM-Pro").match("Ubiquiti UDM-Pro Dream Machine Pro, used once").matched


def test_lots_are_priced_per_unit_at_lower_score():
    result = matcher("U7-Pro").match("Lot of 3 Ubiquiti U7 Pro Access Points NEW")
    assert result.matched
    assert result.quantity == 3
    assert result.score == 0.6


def test_pack_variant_needs_matching_quantity():
    five = matcher("U7-Pro-5", pack=5)
    assert five.match("Ubiquiti U7 Pro 5-Pack WiFi 7 Access Points").matched
    assert five.match("UniFi U7-Pro (5 pack) sealed").matched
    assert not five.match("Ubiquiti U7 Pro single access point").matched


def test_condition_detection():
    assert condition_from_id(1000) == "new"
    assert condition_from_id("1500") == "open_box"
    assert condition_from_id(3000) == "used"
    assert condition_from_id(7000) == "parts"
    assert condition_from_id(None) is None
    assert condition_from_title("U7 Pro open box") == "open_box"
    assert condition_from_title("U7 Pro pre-owned") == "used"
    assert condition_from_title("U7 Pro BNIB") == "new"
    # The API's condition beats title keywords.
    assert matcher("U7-Pro").match("Ubiquiti U7 Pro NEW", condition_id=3000).condition == "used"


@pytest.mark.parametrize(
    ("title", "quantity"),
    [
        ("lot of 4 ubiquiti u7 pro", 4),
        ("Ubiquiti U7 Pro 3-Pack", 3),
        ("(2) Ubiquiti U7 Pro", 2),
        ("2x Ubiquiti U7 Pro", 2),
        ("Ubiquiti U7 Pro qty 6", 6),
        ("Ubiquiti U7 Pro 4x4 MIMO", 1),
        ("Ubiquiti U7 Pro WiFi 7", 1),
    ],
)
def test_detect_quantity(title: str, quantity: int):
    assert detect_quantity(title) == quantity


def test_base_sku():
    assert base_sku("U7-Pro-5", 5) == "U7-Pro"
    assert base_sku("USW-Flex-Mini-5", 5) == "USW-Flex-Mini"
    assert base_sku("UBB-XG", 2) == "UBB-XG"


# ── valuation ─────────────────────────────────────────────────────────────

SETTINGS = MarketSettings(_env_file=None)  # type: ignore[call-arg]


def sold(price: int, days_ago: float = 5, **kw: object) -> Comp:
    values: dict[str, object] = {
        "source": "ebay_sold",
        "price_cents": price,
        "shipping_cents": 0,
        "quantity": 1,
        "condition": "new",
        "sold": True,
        "sold_at": T0 - timedelta(days=days_ago),
        "observed_at": T0,
    }
    values.update(kw)
    return Comp(**values)  # type: ignore[arg-type]


def active(price: int, hours_ago: float = 1, **kw: object) -> Comp:
    values: dict[str, object] = {
        "source": "ebay_active",
        "sold": False,
        "sold_at": None,
        "observed_at": T0 - timedelta(hours=hours_ago),
    }
    values.update(kw)
    return sold(price, **values)


def test_profit_math_from_sold_comps():
    comps = [sold(p) for p in (26000, 27000, 28000, 29000, 30000, 31000, 32000, 33000)]
    result = value_from_comps(18900, comps, SETTINGS, T0)
    assert result.basis == "sold"
    assert result.resale_cents == 29500
    assert result.fees_cents == round(29500 * 0.136) + 40  # 4052
    assert result.shipping_cents == 1200
    assert result.cost_cents == 18900
    assert result.net_cents == 29500 - 4052 - 1200 - 18900  # 5348
    assert result.roi == round(5348 / 18900, 4)
    assert result.confidence == "high"
    assert result.sold_30d == 8
    assert result.score > 0


def test_active_listings_basis_uses_lower_quartile():
    comps = [active(p) for p in (30000, 32000, 34000, 36000)]
    result = value_from_comps(18900, comps, SETTINGS, T0)
    assert result.basis == "active"
    assert result.resale_cents == percentile([30000, 32000, 34000, 36000], 0.25)
    assert result.confidence == "low"


def test_filters_conditions_age_outliers_and_lots():
    comps = [
        sold(30000),
        sold(30000),
        sold(31000),
        sold(9000, condition="used"),  # wrong condition
        sold(30000, days_ago=200),  # outside the sold window
        sold(500),  # implausibly cheap vs retail: likely an accessory
        sold(90000, quantity=3),  # lot: $300/unit, counts
    ]
    result = value_from_comps(18900, comps, SETTINGS, T0)
    assert result.sold_count == 4
    assert result.resale_cents == 30000


def test_not_enough_comps_means_no_valuation():
    result = value_from_comps(18900, [sold(30000), sold(31000)], SETTINGS, T0)
    assert result.basis is None
    assert result.net_cents is None
    assert result.score == 0
    assert result.confidence == "none"


def test_manual_basis_and_tax():
    settings = MarketSettings(_env_file=None, purchase_tax_rate=0.08)  # type: ignore[call-arg]
    comps = [sold(p, source="manual") for p in (25000, 26000, 27000)]
    result = value_from_comps(20000, comps, settings, T0)
    assert result.basis == "manual"
    assert result.cost_cents == 21600
    assert result.manual_count == 3


def test_deal_threshold():
    comps = [sold(p) for p in (30000, 31000, 32000)]
    good = value_from_comps(18900, comps, SETTINGS, T0)
    assert is_deal(good, available=True, settings=SETTINGS)
    assert not is_deal(good, available=False, settings=SETTINGS)
    thin = value_from_comps(25000, comps, SETTINGS, T0)
    assert not is_deal(thin, available=True, settings=SETTINGS)


def test_helpers():
    assert percentile([1, 2, 3, 4], 0.5) == 2  # rounds 2.5 to even
    assert percentile([], 0.5) is None
    assert drop_outliers([100, 101, 102, 103, 104, 500], None) == [100, 101, 102, 103, 104]
    assert opportunity_score(-100, 0.5, "high", 0.5) == 0
    assert opportunity_score(5000, 0.25, "high", None) == 50 * 0.5 * 1.0 * 0.7


# ── regressions from the live eBay audit (2026-11-07, real titles) ──────────


@pytest.mark.parametrize(
    ("sku", "name", "title", "reason"),
    [
        (
            "UCG-Fiber",
            "Cloud Gateway Fiber",
            "Rack mount Ubiquiti UCG-fiber 19inch keystones -add 2.nd device - colo",
            "accessory",
        ),
        (
            "UCG-Fiber",
            "Cloud Gateway Fiber",
            "Ubiquiti Cloud Gateway Fiber 1U Rack Mount UCG Fiber",
            "accessory",
        ),
        (
            "UCG-Fiber",
            "Cloud Gateway Fiber",
            "Ubiquiti UCG Fiber 1U Side Mount Rack Mount",
            "accessory",
        ),
        (
            "U7-Pro-Wall",
            "Access Point U7 Pro Wall",
            "Ubiquiti U7 Pro Wall Paintable Cover - COVER ONLY, NO ACCESS POINT",
            "accessory",
        ),
        (
            "U7-Pro-Wall",
            "Access Point U7 Pro Wall",
            "Ubiquiti U7 Pro Wall Table Stand Network Accessory UACC-U7-PRO-WALLTS",
            "accessory",
        ),
        (
            "U7-Pro-Wall",
            "Access Point U7 Pro Wall",
            "Ubiquiti U7 Pro Wall Paintable Flush Mount - MOUNT ONLY, NO AP - NEW SEALED",
            "accessory",
        ),
        (
            "UCG-Fiber",
            "Cloud Gateway Fiber",
            "Ubiquiti Cloud Gateway Fiber UCG-FIBER & 1 TB SSD Module UACC-SSD-1TB",
            "bundle",
        ),
        (
            "UCG-Fiber",
            "Cloud Gateway Fiber",
            "Ubiquiti Networks Unifi Cloud Gateway Fiber UCG-Fiber (30W) w/ NVME tray",
            "bundle",
        ),
    ],
)
def test_live_audit_false_matches_are_rejected(sku: str, name: str, title: str, reason: str):
    result = VariantMatcher(sku, 1, [*CATALOG, "UCG-Fiber"], name).match(title)
    assert not result.matched
    assert result.reason == reason


@pytest.mark.parametrize(
    ("sku", "name", "title"),
    [
        (
            "UDM-Pro-Max",
            "Dream Machine Pro Max",
            "Ubiquiti Networks UDM-PRO-MAX Professional-Grade Rack-mount Cloud Gateway",
        ),
        (
            "U7-Pro",
            "Access Point U7 Pro",
            "Ubiquiti Networks U7-Pro-US WiFi 7 Ceiling AP (includes mount)",
        ),
        ("UCG-Fiber", "Cloud Gateway Fiber", "Ubiquiti Cloud Gateway Fiber UCG-FIBER - NEW SEALED"),
        (
            "U7-Pro",
            "Access Point U7 Pro",
            "Ubiquiti UBI-U7-PRO UniFi 7 Pro Ceiling Mount Access Point Wi-Fi 7 2.5GbE",
        ),
        (
            "UNVR-Pro",
            "Network Video Recorder Pro",
            "Ubiquiti UNVR-Pro Network Video Recorder Pro - No HDD",
        ),
        (
            "UVC-G4 Doorbell Pro PoE Kit",
            "G4 Doorbell Pro PoE Kit",
            "Ubiquiti UVC-G4 Doorbell Pro PoE Kit New",
        ),
        ("UACC-SSD-1TB", "SSD 1TB", "Ubiquiti UACC-SSD-1TB NVMe SSD 1TB"),
    ],
)
def test_live_audit_real_devices_still_match(sku: str, name: str, title: str):
    result = VariantMatcher(sku, 1, [*CATALOG, "UCG-Fiber"], name).match(title)
    assert result.matched, result.reason


def test_pack_sku_written_out_is_a_pack():
    # Live: "U7-PRO-5-US" ($1,150) was counted as a single U7-Pro.
    single = matcher("U7-Pro").match("U7-PRO-5-US")
    assert single.matched
    assert single.quantity == 5
    assert single.score == 0.6  # priced per unit, lower weight
    five = matcher("U7-Pro-5", pack=5).match("U7-PRO-5-US")
    assert five.matched
    assert five.quantity == 5


def test_digit_after_sku_that_is_a_spec_is_not_a_pack():
    result = matcher("U7-Pro").match("Ubiquiti U7 Pro 5 GHz WiFi 7 Access Point")
    assert result.matched
    assert result.quantity == 1
    # A digit that isn't a catalog pack size is ignored too.
    assert matcher("U7-Pro").match("Ubiquiti U7 Pro 7 WiFi Access Point").quantity == 1
