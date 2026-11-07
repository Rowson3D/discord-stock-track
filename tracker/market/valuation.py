"""Pure resale math: comps → market stats → valuation and opportunity score.

Two stages, because eBay listings are never stored (eBay API compliance: the app declares it
does not persist eBay data):

1. `summarize()` turns comps into aggregate `MarketStats`. eBay results are summarized in
   memory right after fetching and only the stats are kept. Manual comps (your own data) are
   stored and summarized on demand.
2. `compute_valuation()` combines stats with the current retail price:

    gross  = resale per unit (price + buyer-paid shipping)
    fees   = gross × fee_rate + fixed fee
    cost   = retail × (1 + purchase tax) + inbound shipping
    net    = gross − fees − outbound shipping − cost
    ROI    = net / cost

Resale basis, in order: eBay sold median, manual sold median, eBay active 25th percentile
(you'd have to undercut asking prices to sell quickly). Each needs `min_comps` data points.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tracker.settings import MarketSettings

RESALE_CONDITIONS = frozenset({"new", "open_box", "unknown"})
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.75, "low": 0.5, "none": 0.0}
SANE_LOW, SANE_HIGH = 0.3, 4.0  # Comp per-unit price relative to retail.


@dataclass(frozen=True, slots=True)
class Comp:
    source: str
    price_cents: int
    shipping_cents: int | None
    quantity: int
    condition: str
    sold: bool
    sold_at: datetime | None
    observed_at: datetime
    match_score: float = 1.0

    @property
    def unit_cents(self) -> int:
        return (self.price_cents + (self.shipping_cents or 0)) // max(1, self.quantity)


@dataclass(frozen=True, slots=True)
class MarketStats:
    """Aggregate per-unit prices. Contains no listing-level data."""

    sold_count: int = 0
    sold_30d: int = 0
    sold_median_cents: int | None = None
    sold_p25_cents: int | None = None
    sold_p75_cents: int | None = None
    active_count: int = 0
    active_median_cents: int | None = None
    active_p25_cents: int | None = None
    active_low_cents: int | None = None

    def without_active(self) -> MarketStats:
        return MarketStats(
            sold_count=self.sold_count,
            sold_30d=self.sold_30d,
            sold_median_cents=self.sold_median_cents,
            sold_p25_cents=self.sold_p25_cents,
            sold_p75_cents=self.sold_p75_cents,
        )


EMPTY = MarketStats()


@dataclass(frozen=True, slots=True)
class ValuationResult:
    retail_cents: int | None
    basis: str | None
    resale_cents: int | None
    fees_cents: int | None
    shipping_cents: int | None
    cost_cents: int | None
    net_cents: int | None
    roi: float | None
    sold_count: int
    sold_30d: int
    active_count: int
    manual_count: int
    sold_median_cents: int | None
    sold_p25_cents: int | None
    sold_p75_cents: int | None
    active_median_cents: int | None
    active_low_cents: int | None
    sell_through: float | None
    confidence: str
    score: float


def percentile(values: Sequence[int], fraction: float) -> int | None:
    """Linear-interpolated percentile (fraction in 0..1)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def drop_outliers(values: list[int], retail_cents: int | None) -> list[int]:
    if retail_cents:
        values = [v for v in values if SANE_LOW * retail_cents <= v <= SANE_HIGH * retail_cents]
    if len(values) >= 5:
        q1, q3 = percentile(values, 0.25) or 0, percentile(values, 0.75) or 0
        spread = (q3 - q1) * 1.5
        values = [v for v in values if q1 - spread <= v <= q3 + spread]
    return values


def summarize(
    comps: Sequence[Comp], retail_cents: int | None, settings: MarketSettings, now: datetime
) -> MarketStats:
    usable = [c for c in comps if c.condition in RESALE_CONDITIONS and c.match_score >= 0.5]
    sold_cutoff = now - timedelta(days=settings.sold_window_days)
    sold = [c for c in usable if c.sold and (c.sold_at or c.observed_at) >= sold_cutoff]
    active = [c for c in usable if not c.sold]
    month = now - timedelta(days=30)

    sold_units = drop_outliers([c.unit_cents for c in sold], retail_cents)
    active_units = drop_outliers([c.unit_cents for c in active], retail_cents)
    return MarketStats(
        sold_count=len(sold_units),
        sold_30d=sum(1 for c in sold if (c.sold_at or c.observed_at) >= month),
        sold_median_cents=percentile(sold_units, 0.5),
        sold_p25_cents=percentile(sold_units, 0.25),
        sold_p75_cents=percentile(sold_units, 0.75),
        active_count=len(active_units),
        active_median_cents=percentile(active_units, 0.5),
        active_p25_cents=percentile(active_units, 0.25),
        active_low_cents=min(active_units) if active_units else None,
    )


def confidence_for(sold: int, active: int, min_comps: int) -> str:
    if sold >= max(8, min_comps):
        return "high"
    if sold >= min_comps or active >= max(8, min_comps):
        return "medium"
    if sold or active:
        return "low"
    return "none"


def opportunity_score(
    net_cents: int | None, roi: float | None, confidence: str, sell_through: float | None
) -> float:
    """Dollars of profit, tempered by ROI, confidence, and how fast the item sells."""
    if not net_cents or net_cents <= 0 or roi is None:
        return 0.0
    liquidity = 0.5 + 0.5 * sell_through if sell_through is not None else 0.7
    return round(
        net_cents / 100 * math.sqrt(min(roi, 1.0)) * CONFIDENCE_WEIGHT[confidence] * liquidity, 2
    )


def compute_valuation(
    retail_cents: int | None,
    ebay: MarketStats | None,
    manual: MarketStats | None,
    settings: MarketSettings,
) -> ValuationResult:
    ebay, manual = ebay or EMPTY, manual or EMPTY
    minimum = settings.min_comps

    basis: str | None = None
    resale: int | None = None
    sold_view = ebay  # Which sold stats to report.
    if ebay.sold_count >= minimum:
        basis, resale = "sold", ebay.sold_median_cents
    elif manual.sold_count >= minimum:
        basis, resale, sold_view = "manual", manual.sold_median_cents, manual
    elif ebay.active_count >= minimum:
        basis, resale = "active", ebay.active_p25_cents
    elif manual.sold_count:
        sold_view = manual

    fees = shipping = cost = net = None
    roi = None
    if resale is not None and retail_cents:
        fees = round(resale * settings.fee_rate) + settings.fixed_fee_cents
        shipping = settings.outbound_shipping_cents
        cost = round(retail_cents * (1 + settings.purchase_tax_rate))
        cost += settings.inbound_shipping_cents
        net = resale - fees - shipping - cost
        roi = round(net / cost, 4) if cost else None

    sell_through = None
    if ebay.sold_30d or (ebay.sold_count and ebay.active_count):
        total = ebay.sold_30d + ebay.active_count
        sell_through = round(ebay.sold_30d / total, 3) if total else None

    confidence = (
        confidence_for(sold_view.sold_count, ebay.active_count, minimum) if basis else "none"
    )
    return ValuationResult(
        retail_cents=retail_cents,
        basis=basis,
        resale_cents=resale,
        fees_cents=fees,
        shipping_cents=shipping,
        cost_cents=cost,
        net_cents=net,
        roi=roi,
        sold_count=sold_view.sold_count,
        sold_30d=sold_view.sold_30d,
        active_count=ebay.active_count,
        manual_count=manual.sold_count,
        sold_median_cents=sold_view.sold_median_cents,
        sold_p25_cents=sold_view.sold_p25_cents,
        sold_p75_cents=sold_view.sold_p75_cents,
        active_median_cents=ebay.active_median_cents,
        active_low_cents=ebay.active_low_cents,
        sell_through=sell_through,
        confidence=confidence,
        score=opportunity_score(net, roi, confidence, sell_through),
    )


def value_from_comps(
    retail_cents: int | None, comps: Sequence[Comp], settings: MarketSettings, now: datetime
) -> ValuationResult:
    """Convenience: split comps by source, summarize each, and value them."""
    manual = [c for c in comps if c.source == "manual"]
    ebay = [c for c in comps if c.source != "manual"]
    return compute_valuation(
        retail_cents,
        summarize(ebay, retail_cents, settings, now),
        summarize(manual, retail_cents, settings, now),
        settings,
    )


def is_deal(result: ValuationResult, available: bool, settings: MarketSettings) -> bool:
    return bool(
        available
        and result.net_cents is not None
        and result.roi is not None
        and result.net_cents >= settings.deal_min_profit_cents
        and result.roi >= settings.deal_min_roi
        and result.confidence in ("medium", "high")
    )
