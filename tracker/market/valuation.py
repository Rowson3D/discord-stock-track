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

Active asking prices are **capped** while the store has the item in stock (basis `capped`):
nobody rational pays a reseller far more than the store price. Profit from in-stock items
must come from real sales data or from **scarcity pricing**: eBay prices recorded on days the
store was sold out. Deals and rankings use that credible "flip" value only.
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
    seller: str | None = None  # In-memory only; collapses one seller's many listings.

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
    active_count: int = 0  # Distinct sellers (each counted once, at their cheapest).
    active_median_cents: int | None = None
    active_p25_cents: int | None = None
    active_low_cents: int | None = None
    active_listings: int = 0  # Matching listings before collapsing by seller.

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
    # What it resells for while the store is sold out (from daily history).
    scarcity_days: int = 0
    scarcity_resale_cents: int | None = None
    scarcity_net_cents: int | None = None
    scarcity_roi: float | None = None
    # The number deals and rankings trust: real sales, your own sales, or proven scarcity.
    flip_basis: str | None = None  # sold | manual | scarcity
    flip_net_cents: int | None = None
    flip_roi: float | None = None
    flip_confidence: str = "none"


@dataclass(frozen=True, slots=True)
class ScarcityStats:
    """eBay asking prices on days the store itself was sold out.

    Asking prices are only meaningful when the buyer has no cheaper option. On sold-out
    days they're the market a restock buyer actually sells into.
    """

    days: int
    resale_cents: int | None  # Median of those days' 25th-percentile asking price.
    sellers: int  # Median distinct sellers on those days.


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
    active = dedupe_sellers([c for c in usable if not c.sold])
    active_listings = sum(1 for c in usable if not c.sold)
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
        active_listings=active_listings,
    )


def dedupe_sellers(comps: Sequence[Comp]) -> list[Comp]:
    """One active data point per seller: their cheapest unit price.

    A reseller with ten near-identical listings is one opinion about price, not ten, and
    shouldn't inflate the comp count or the confidence. Unknown sellers count individually.
    """
    best: dict[str, Comp] = {}
    for index, comp in enumerate(comps):
        key = comp.seller or f"#unknown-{index}"
        if key not in best or comp.unit_cents < best[key].unit_cents:
            best[key] = comp
    return list(best.values())


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


def _profit(
    resale: int | None, retail_cents: int | None, settings: MarketSettings
) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    """(fees, shipping, cost, net, roi) for selling at `resale` after buying at retail."""
    if resale is None or not retail_cents:
        return None, None, None, None, None
    fees = round(resale * settings.fee_rate) + settings.fixed_fee_cents
    shipping = settings.outbound_shipping_cents
    cost = round(retail_cents * (1 + settings.purchase_tax_rate)) + settings.inbound_shipping_cents
    net = resale - fees - shipping - cost
    return fees, shipping, cost, net, (round(net / cost, 4) if cost else None)


def in_stock_ceiling(retail_cents: int, settings: MarketSettings) -> int:
    """The most a rational buyer pays a reseller when the store has it in stock."""
    return round(retail_cents * (1 + settings.buyer_tax_rate + settings.convenience_premium))


def scarcity_confidence(scarcity: ScarcityStats | None, settings: MarketSettings) -> str:
    if scarcity is None or scarcity.resale_cents is None:
        return "none"
    if scarcity.days >= max(4, settings.scarcity_min_days) and scarcity.sellers >= 3:
        return "medium"
    if scarcity.days >= settings.scarcity_min_days:
        return "low"
    return "none"


def compute_valuation(
    retail_cents: int | None,
    ebay: MarketStats | None,
    manual: MarketStats | None,
    settings: MarketSettings,
    *,
    available: bool = False,
    scarcity: ScarcityStats | None = None,
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
        if available and retail_cents and resale is not None:
            # Asking prices for something the store sells right now are mostly resellers
            # hoping; nobody rational pays more than the store price plus tax and hassle.
            ceiling = in_stock_ceiling(retail_cents, settings)
            if resale > ceiling:
                basis, resale = "capped", ceiling
    elif manual.sold_count:
        sold_view = manual

    fees, shipping, cost, net, roi = _profit(resale, retail_cents, settings)

    sell_through = None
    if ebay.sold_30d or (ebay.sold_count and ebay.active_count):
        total = ebay.sold_30d + ebay.active_count
        sell_through = round(ebay.sold_30d / total, 3) if total else None

    confidence = (
        confidence_for(sold_view.sold_count, ebay.active_count, minimum) if basis else "none"
    )

    scarce_conf = scarcity_confidence(scarcity, settings)
    scarce_net = scarce_roi = None
    if scarce_conf != "none" and scarcity is not None:
        *_, scarce_net, scarce_roi = _profit(scarcity.resale_cents, retail_cents, settings)

    flip_basis = flip_net = flip_roi = None
    flip_conf = "none"
    if basis in ("sold", "manual") and net is not None:
        flip_basis, flip_net, flip_roi, flip_conf = basis, net, roi, confidence
    elif scarce_net is not None:
        flip_basis, flip_net, flip_roi, flip_conf = "scarcity", scarce_net, scarce_roi, scarce_conf

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
        score=opportunity_score(flip_net, flip_roi, flip_conf, sell_through),
        scarcity_days=scarcity.days if scarcity else 0,
        scarcity_resale_cents=scarcity.resale_cents if scarcity else None,
        scarcity_net_cents=scarce_net,
        scarcity_roi=scarce_roi,
        flip_basis=flip_basis,
        flip_net_cents=flip_net,
        flip_roi=flip_roi,
        flip_confidence=flip_conf,
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
    """Buyable now, and profitable on *credible* evidence only.

    Credible means real sales (eBay sold, your own) or proven scarcity pricing. eBay asking
    prices for something the store has in stock never qualify.
    """
    return bool(
        available
        and result.flip_net_cents is not None
        and result.flip_roi is not None
        and result.flip_net_cents >= settings.deal_min_profit_cents
        and result.flip_roi >= settings.deal_min_roi
        and result.flip_confidence in ("medium", "high")
    )
