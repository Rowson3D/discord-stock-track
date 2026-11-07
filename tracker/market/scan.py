"""On-demand, read-only profit scan: prices catalog items against the live eBay market.

Nothing is written to the database and no events are raised: results exist only in memory
for the report (eBay no-persist compliance; and a scan never fires alerts). Each priced item
costs one eBay Browse call.

Besides single items, it evaluates **pack breakouts**: buying a multi-pack from the store and
selling the units individually, with the per-unit break-even resale price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Listing, Product, Variant
from tracker.domain import StockStatus
from tracker.market.ebay import EbayClient
from tracker.market.matching import VariantMatcher, base_sku
from tracker.market.service import search_query
from tracker.market.valuation import Comp, compute_valuation, summarize
from tracker.settings import MarketSettings


@dataclass(frozen=True, slots=True)
class ScanItem:
    sku: str
    name: str
    status: StockStatus
    restock_eta: date | None
    retail_cents: int
    sellers: int
    listings: int
    resale_cents: int | None
    net_cents: int | None
    roi: float | None
    confidence: str

    @property
    def available(self) -> bool:
        return self.status.is_available


@dataclass(frozen=True, slots=True)
class Breakout:
    pack_sku: str
    pack_size: int
    pack_cents: int
    status: StockStatus
    unit_cost_cents: int
    single_sku: str
    single_resale_cents: int
    unit_net_cents: int
    pack_net_cents: int
    breakeven_unit_cents: int  # Single resale price at which the pack breaks even.
    confidence: str


@dataclass(slots=True)
class ScanReport:
    at: datetime
    priced: int = 0
    calls: int = 0
    items: list[ScanItem] = field(default_factory=list)
    breakouts: list[Breakout] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def valued(self) -> list[ScanItem]:
        return [i for i in self.items if i.net_cents is not None]

    def top(self, limit: int = 10) -> list[ScanItem]:
        return sorted(
            (i for i in self.valued if (i.net_cents or 0) > 0),
            key=lambda i: -(i.net_cents or 0),
        )[:limit]

    def losers(self, limit: int = 5) -> list[ScanItem]:
        return sorted(
            (i for i in self.valued if (i.net_cents or 0) < 0), key=lambda i: i.net_cents or 0
        )[:limit]

    def best_breakouts(self, limit: int = 8) -> list[Breakout]:
        return sorted(
            (b for b in self.breakouts if b.pack_net_cents > 0), key=lambda b: -b.pack_net_cents
        )[:limit]


def breakeven_unit_cents(unit_cost_cents: float, settings: MarketSettings) -> int:
    """Resale price per unit where net = 0, given the fee model."""
    cost = unit_cost_cents * (1 + settings.purchase_tax_rate) + settings.inbound_shipping_cents
    return round(
        (cost + settings.fixed_fee_cents + settings.outbound_shipping_cents)
        / (1 - settings.fee_rate)
    )


async def _candidates(
    session: AsyncSession, min_retail_cents: int, limit: int
) -> tuple[
    list[tuple[Variant, Product, Listing]], list[tuple[Variant, Product, Listing]], list[str]
]:
    rows = (
        await session.execute(
            select(Variant, Product, Listing)
            .join(Product, Variant.product_id == Product.id)
            .join(Listing, Listing.variant_id == Variant.id)
            .where(
                Listing.source == "unifi",
                Listing.region == "us",
                Listing.currency == "USD",
                Listing.price_cents.is_not(None),
                Listing.delisted_at.is_(None),
            )
        )
    ).all()
    catalog = list(await session.scalars(select(Variant.sku)))
    by_sku = {v.sku: (v, p, listing) for v, p, listing in rows}

    def worth(row: tuple[Variant, Product, Listing]) -> bool:
        _, product, listing = row
        return (listing.price_cents or 0) >= min_retail_cents and not (
            product.category or ""
        ).startswith("accessories")

    # Packs whose single version is itself worth scanning (skips $3 cable multi-packs).
    packs = [
        row
        for row in rows
        if row[0].pack_size > 1
        and (single := by_sku.get(base_sku(row[0].sku, row[0].pack_size)))
        and worth(single)
    ]
    wanted: list[tuple[Variant, Product, Listing]] = []
    seen: set[str] = set()
    for pack in packs:  # Breakouts first: they're the unique insight.
        single = by_sku[base_sku(pack[0].sku, pack[0].pack_size)]
        if single[0].sku not in seen:
            wanted.append(single)
            seen.add(single[0].sku)
    singles = sorted(
        (r for r in rows if r[0].pack_size == 1 and worth(r) and r[0].sku not in seen),
        # Buyable now first, then the most expensive (most profit at stake).
        key=lambda r: (not StockStatus(r[2].status).is_available, -(r[2].price_cents or 0)),
    )
    wanted.extend(singles[: max(0, limit - len(wanted))])
    return wanted[:limit], packs, catalog


async def run_scan(
    sessions: async_sessionmaker[AsyncSession],
    ebay: EbayClient,
    settings: MarketSettings,
    *,
    limit: int = 25,
    min_retail_cents: int = 10000,
    now: datetime | None = None,
) -> ScanReport:
    now = now or datetime.now(UTC)
    report = ScanReport(at=now)
    async with sessions() as session:
        wanted, packs, catalog = await _candidates(session, min_retail_cents, limit)

    resale_by_sku: dict[str, ScanItem] = {}
    for variant, product, listing in wanted:
        retail = listing.price_cents or 0
        try:
            results = await ebay.search_active(
                search_query(variant.sku, variant.pack_size), min_price_cents=int(retail * 0.3)
            )
        except Exception as exc:
            report.errors.append(f"{variant.sku}: {exc}")
            if getattr(exc, "status", None) == 429:
                break  # Out of quota: stop rather than hammer.
            continue
        report.calls += 1
        matcher = VariantMatcher(variant.sku, variant.pack_size, catalog, product.name)
        comps: list[Comp] = []
        for result in results:
            match = matcher.match(result.title, result.condition_id)
            if match.matched:
                comps.append(
                    Comp(
                        source="ebay_active",
                        price_cents=result.price_cents,
                        shipping_cents=result.shipping_cents,
                        quantity=match.quantity,
                        condition=match.condition or "unknown",
                        sold=False,
                        sold_at=None,
                        observed_at=now,
                        match_score=match.score,
                        seller=result.seller,
                    )
                )
        stats = summarize(comps, retail, settings, now)
        value = compute_valuation(retail, stats, None, settings)
        item = ScanItem(
            sku=variant.sku,
            name=product.name,
            status=StockStatus(listing.status),
            restock_eta=listing.restock_eta,
            retail_cents=retail,
            sellers=stats.active_count,
            listings=stats.active_listings,
            resale_cents=value.resale_cents,
            net_cents=value.net_cents,
            roi=value.roi,
            confidence=value.confidence,
        )
        report.items.append(item)
        resale_by_sku[variant.sku] = item
        report.priced += 1

    for variant, _, listing in packs:
        single = resale_by_sku.get(base_sku(variant.sku, variant.pack_size))
        if single is None or single.resale_cents is None or not listing.price_cents:
            continue
        unit_cost = listing.price_cents / variant.pack_size
        unit_fees = single.resale_cents * settings.fee_rate + settings.fixed_fee_cents
        unit_net = (
            single.resale_cents
            - unit_fees
            - settings.outbound_shipping_cents
            - unit_cost * (1 + settings.purchase_tax_rate)
            - settings.inbound_shipping_cents
        )
        report.breakouts.append(
            Breakout(
                pack_sku=variant.sku,
                pack_size=variant.pack_size,
                pack_cents=listing.price_cents,
                status=StockStatus(listing.status),
                unit_cost_cents=round(unit_cost),
                single_sku=single.sku,
                single_resale_cents=single.resale_cents,
                unit_net_cents=round(unit_net),
                pack_net_cents=round(unit_net * variant.pack_size),
                breakeven_unit_cents=breakeven_unit_cents(unit_cost, settings),
                confidence=single.confidence,
            )
        )
    return report
