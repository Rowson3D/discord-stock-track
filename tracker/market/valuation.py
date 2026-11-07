"""Pure resale math: comps + retail price + fee model → valuation and opportunity score.

    gross  = resale per unit (price + buyer-paid shipping)
    fees   = gross × fee_rate + fixed fee
    cost   = retail × (1 + purchase tax) + inbound shipping
    net    = gross − fees − outbound shipping − cost
    ROI    = net / cost

Resale basis: the median of recent *sold* comps (eBay sold or manual) when there are enough;
otherwise the 25th percentile of fresh *active* listings (you'd have to undercut asking
prices to sell quickly), otherwise nothing.
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
    comps: Sequence[Comp],
    settings: MarketSettings,
    now: datetime,
) -> ValuationResult:
    usable = [c for c in comps if c.condition in RESALE_CONDITIONS and c.match_score >= 0.5]
    sold_cutoff = now - timedelta(days=settings.sold_window_days)
    sold = [c for c in usable if c.sold and (c.sold_at or c.observed_at) >= sold_cutoff]
    month = now - timedelta(days=30)
    active_cutoff = now - timedelta(hours=settings.active_max_age_hours)
    active = [c for c in usable if not c.sold and c.observed_at >= active_cutoff]

    sold_units = drop_outliers([c.unit_cents for c in sold], retail_cents)
    active_units = drop_outliers([c.unit_cents for c in active], retail_cents)
    sold_30d = sum(1 for c in sold if (c.sold_at or c.observed_at) >= month)
    manual_count = sum(1 for c in sold if c.source == "manual")

    basis: str | None = None
    resale: int | None = None
    if len(sold_units) >= settings.min_comps:
        basis = "manual" if manual_count == len(sold) else "sold"
        resale = percentile(sold_units, 0.5)
    elif len(active_units) >= settings.min_comps:
        basis, resale = "active", percentile(active_units, 0.25)

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
    if sold_30d or (sold and active):
        sell_through = round(sold_30d / (sold_30d + len(active_units)), 3) if sold_30d else 0.0

    confidence = confidence_for(len(sold_units), len(active_units), settings.min_comps)
    return ValuationResult(
        retail_cents=retail_cents,
        basis=basis,
        resale_cents=resale,
        fees_cents=fees,
        shipping_cents=shipping,
        cost_cents=cost,
        net_cents=net,
        roi=roi,
        sold_count=len(sold_units),
        sold_30d=sold_30d,
        active_count=len(active_units),
        manual_count=manual_count,
        sold_median_cents=percentile(sold_units, 0.5),
        sold_p25_cents=percentile(sold_units, 0.25),
        sold_p75_cents=percentile(sold_units, 0.75),
        active_median_cents=percentile(active_units, 0.5),
        active_low_cents=min(active_units) if active_units else None,
        sell_through=sell_through,
        confidence=confidence if basis else "none",
        score=opportunity_score(net, roi, confidence if basis else "none", sell_through),
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
