"""Read a candidate's eBay market (aggregates only) and estimate the flip honestly.

Matching is stricter than "title contains words": the model number must appear as its own
token run (so "RTX 5090" never matches "RTX 5090 Ti" unless that's the product), and parts,
empty boxes, lots, and accessories *for* the product are rejected. Prices are one per seller
(their cheapest), with outliers dropped. Nothing listing-level is stored.

The estimate applies the rule from the UniFi fake-deal fix: if the product can be bought at
regular price somewhere, resale is capped at that price plus tax and convenience.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tracker.discovery.identity import clean_title, compact, pick_model, tokens
from tracker.market.ebay import EbayClient, EbayItem
from tracker.market.matching import BOX_ONLY_PHRASES, PARTS_PHRASES, detect_quantity
from tracker.market.valuation import _profit, drop_outliers, in_stock_ceiling, percentile
from tracker.settings import DiscoverySettings, MarketSettings

ACCESSORY_WORDS = frozenset(
    {
        "case", "cover", "skin", "protector", "charger", "cable", "adapter", "mount", "stand",
        "strap", "sleeve", "decal", "sticker", "bag", "pouch", "holder", "dock", "grip", "shell",
        "faceplate", "replacement", "parts", "remote", "earpads", "cushions", "tips", "lens",
        "screen", "glass", "film", "keycaps", "manual", "box", "minifigure", "minifigures",
        "minifig", "minifigs", "instructions", "figure", "figures", "block", "waterblock",
        "backplate", "shroud", "fan", "fans", "riser",
    }
)  # fmt: skip
# A whole system built around the product ("Gaming PC with RTX 5090") isn't the product.
SYSTEM_WORDS = frozenset(
    {
        "pc", "desktop", "computer", "laptop", "notebook", "prebuilt", "workstation", "tower",
        "build", "system", "rig", "ram", "ssd", "nvme", "ddr4", "ddr5", "ryzen", "i5", "i7",
        "i9", "legion", "alienware", "omen", "predator",
    }
)  # fmt: skip
# Right after the model, these mean a different (usually pricier) model.
MODEL_MODIFIERS = frozenset(
    {"pro", "max", "ultra", "plus", "mini", "lite", "se", "ti", "super", "xl", "slim", "oled"}
)
EXTRA_REJECTS = ("replacement for", "compatible with", "for use with", "read description")


@dataclass(frozen=True, slots=True)
class MarketReading:
    query: str
    fetched: int
    matched: int
    sellers: int
    listings: int
    p25_cents: int | None
    median_cents: int | None
    low_cents: int | None


@dataclass(frozen=True, slots=True)
class Estimate:
    resale_cents: int | None
    net_cents: int | None
    roi: float | None
    confidence: str
    capped: bool


def search_query(title: str, model: str | None = None) -> str:
    chosen = pick_model(title, model)
    if chosen:
        return chosen
    words = clean_title(title).split()
    return " ".join(words[:7])


def _window(item_tokens: list[str], target: str) -> int | None:
    """End index of a run of consecutive tokens that spells `target` exactly, else None."""
    for start in range(len(item_tokens)):
        joined = ""
        for end in range(start, min(start + 4, len(item_tokens))):
            joined += item_tokens[end]
            if joined == target:
                return end + 1
            if not target.startswith(joined):
                break
    return None


def matches(item_title: str, candidate_title: str, model: str | None) -> bool:
    lowered = item_title.lower()
    if any(p in lowered for p in (*PARTS_PHRASES, *BOX_ONLY_PHRASES, *EXTRA_REJECTS)):
        return False
    if detect_quantity(item_title) > 1:
        return False
    item = tokens(item_title)
    own = set(tokens(candidate_title))
    if model:
        end = _window(item, compact(model))
        if end is None:
            return False
        following = item[end] if end < len(item) else None
        if following in MODEL_MODIFIERS and following not in own:
            return False
    else:
        wanted = [w for w in tokens(clean_title(candidate_title)) if len(w) > 1][:6]
        if not wanted or sum(w in item for w in wanted) < math.ceil(0.8 * len(wanted)):
            return False
    words = set(item)
    if SYSTEM_WORDS & words and not SYSTEM_WORDS & own:
        return False
    # Reject accessories ("case for …", "replacement remote …") unless the product is one.
    return bool(ACCESSORY_WORDS & own) or not (ACCESSORY_WORDS & words)


def summarize_items(
    items: Sequence[EbayItem], candidate_title: str, model: str | None, retail_cents: int | None
) -> tuple[int, int, int, list[int]]:
    """(matched, sellers, listings, per-seller prices after outlier removal)."""
    good = [i for i in items if matches(i.title, candidate_title, model)]
    best: dict[str, int] = {}
    for index, item in enumerate(good):
        total = item.price_cents + (item.shipping_cents or 0)
        key = item.seller or f"#unknown-{index}"
        best[key] = min(total, best.get(key, total))
    values = sorted(best.values())
    if retail_cents:
        values = drop_outliers(values, retail_cents)
    elif values:
        # No store price to anchor on: use the market's own median.
        middle = values[len(values) // 2]
        values = [v for v in values if 0.3 * middle <= v <= 4 * middle]
    return len(good), len(values), len(good), values


async def read_market(
    ebay: EbayClient,
    title: str,
    *,
    model: str | None,
    retail_cents: int | None,
) -> MarketReading:
    query = search_query(title, model)
    chosen = pick_model(title, model)
    floor = round(retail_cents * 0.3) if retail_cents else None
    items = await ebay.search_active(query, min_price_cents=floor, limit=100)
    matched, sellers, listings, values = summarize_items(items, title, chosen, retail_cents)
    return MarketReading(
        query=query,
        fetched=len(items),
        matched=matched,
        sellers=sellers,
        listings=listings,
        p25_cents=percentile(values, 0.25),
        median_cents=percentile(values, 0.5),
        low_cents=min(values) if values else None,
    )


def estimate(
    reading: MarketReading,
    *,
    retail_cents: int | None,
    regular_cents: int | None,
    available: bool | None,
    market: MarketSettings,
    settings: DiscoverySettings,
) -> Estimate:
    resale = reading.p25_cents
    if resale is None:
        return Estimate(None, None, None, "none", False)
    capped = False
    cap_base = regular_cents or retail_cents
    if available is not False and cap_base:
        # Anyone can still buy it at (regular) retail, so asking prices above that aren't real.
        ceiling = in_stock_ceiling(cap_base, market)
        if resale > ceiling:
            resale, capped = ceiling, True
    _, _, _, net, roi = _profit(resale, retail_cents, market)
    if reading.sellers >= settings.min_sellers:
        confidence = "medium" if available is False else "low"
    else:
        confidence = "low" if reading.sellers else "none"
    return Estimate(resale, net, roi, confidence, capped)


def score(net_cents: int | None, roi: float | None, heat: float, sellers: int) -> float:
    if net_cents is None or net_cents <= 0 or roi is None or roi <= 0:
        return 0.0
    depth = min(1.0, sellers / 5)
    return round(net_cents / 100 * math.sqrt(roi) * (1 + math.log1p(max(heat, 0))) * depth, 2)


def decayed_heat(heat: float, last_seen: datetime, now: datetime) -> float:
    """Heat halves every 3 days without a new signal."""
    days = max(0.0, (now - last_seen).total_seconds() / 86400)
    return heat * 0.5 ** (days / 3)
