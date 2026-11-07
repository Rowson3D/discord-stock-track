"""Derived history metrics: how fast things sell out, how long they stay gone, eBay trends.

These are the raw ingredients of grail detection (docs/GRAILS.md): sell-out speed, long
out-of-stock spells, and supply/price momentum on eBay.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import EbayStatsHistory, StockObservation
from tracker.domain import StockStatus
from tracker.market.valuation import ScarcityStats


@dataclass(frozen=True, slots=True)
class StockPoint:
    at: datetime
    status: StockStatus


@dataclass(slots=True)
class StockHistory:
    first_seen: datetime | None = None
    restocks: int = 0
    # Completed in-stock windows that started with a *restock* (we saw it come back).
    sellout_times: list[timedelta] = field(default_factory=list)
    available_since: datetime | None = None
    unavailable_since: datetime | None = None
    # True when the current out-of-stock spell began before tracking started.
    unavailable_at_least: bool = False

    @property
    def median_sellout(self) -> timedelta | None:
        if not self.sellout_times:
            return None
        return timedelta(seconds=statistics.median(t.total_seconds() for t in self.sellout_times))

    def out_of_stock_for(self, now: datetime) -> timedelta | None:
        return now - self.unavailable_since if self.unavailable_since else None


def summarize_stock(points: Sequence[StockPoint]) -> StockHistory:
    """Change-only observations (oldest first) → restock/sell-out metrics.

    The first observation is the baseline: if the item was already in stock we don't know
    when that window started, so it isn't counted as a sell-out time.
    """
    history = StockHistory()
    known = [p for p in points if p.status.is_known]
    if not known:
        return history
    history.first_seen = known[0].at
    restocked_at: datetime | None = None
    previous: StockStatus | None = None
    for point in known:
        available = point.status.is_available
        if previous is None:
            if available:
                history.available_since = point.at
            else:
                history.unavailable_since = point.at
                history.unavailable_at_least = True
        elif available and not previous.is_available:
            history.restocks += 1
            restocked_at = point.at
            history.available_since = point.at
            history.unavailable_since = None
            history.unavailable_at_least = False
        elif not available and previous.is_available:
            if restocked_at is not None:
                history.sellout_times.append(point.at - restocked_at)
            restocked_at = None
            history.available_since = None
            history.unavailable_since = point.at
            history.unavailable_at_least = False
        previous = point.status
    return history


async def stock_history(session: AsyncSession, listing_id: int) -> StockHistory:
    rows = await session.execute(
        select(StockObservation.observed_at, StockObservation.status)
        .where(StockObservation.listing_id == listing_id)
        .order_by(StockObservation.observed_at, StockObservation.id)
    )
    return summarize_stock([StockPoint(at, StockStatus(status)) for at, status in rows.all()])


@dataclass(frozen=True, slots=True)
class EbayTrend:
    days: int  # Actual span between the two snapshots compared.
    first_day: date
    last_day: date
    sellers_then: int
    sellers_now: int
    p25_then: int | None
    p25_now: int | None
    snapshots: int

    @property
    def sellers_change(self) -> float | None:
        if not self.sellers_then:
            return None
        return (self.sellers_now - self.sellers_then) / self.sellers_then

    @property
    def price_change(self) -> float | None:
        if not self.p25_then or self.p25_now is None:
            return None
        return (self.p25_now - self.p25_then) / self.p25_then


async def ebay_trend(session: AsyncSession, variant_id: int, *, days: int = 7) -> EbayTrend | None:
    """Compare the latest daily snapshot with the one closest to `days` earlier."""
    rows = list(
        await session.scalars(
            select(EbayStatsHistory)
            .where(EbayStatsHistory.variant_id == variant_id)
            .order_by(EbayStatsHistory.day)
        )
    )
    if len(rows) < 2:
        return None
    latest = rows[-1]
    target = latest.day - timedelta(days=days)
    earlier = min(rows[:-1], key=lambda r: abs((r.day - target).days))
    return EbayTrend(
        days=(latest.day - earlier.day).days,
        first_day=earlier.day,
        last_day=latest.day,
        sellers_then=earlier.active_count,
        sellers_now=latest.active_count,
        p25_then=earlier.active_p25_cents,
        p25_now=latest.active_p25_cents,
        snapshots=len(rows),
    )


def humanize(delta: timedelta | None) -> str:
    if delta is None:
        return "—"
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d"


async def scarcity_stats(
    session: AsyncSession,
    variant_id: int,
    listing_id: int,
    *,
    now: datetime,
    window_days: int = 90,
) -> ScarcityStats | None:
    """eBay aggregates from days when the retail listing was sold out at snapshot time."""
    rows = list(
        await session.scalars(
            select(EbayStatsHistory)
            .where(
                EbayStatsHistory.variant_id == variant_id,
                EbayStatsHistory.day >= now.date() - timedelta(days=window_days),
            )
            .order_by(EbayStatsHistory.day)
        )
    )
    if not rows:
        return None
    observations = (
        await session.execute(
            select(StockObservation.observed_at, StockObservation.status)
            .where(StockObservation.listing_id == listing_id)
            .order_by(StockObservation.observed_at, StockObservation.id)
        )
    ).all()
    times = [at for at, _ in observations]

    scarce = []
    for row in rows:
        index = bisect_right(times, row.fetched_at) - 1
        if index < 0:
            continue  # No stock knowledge yet at that moment.
        status = StockStatus(observations[index][1])
        if status.is_known and not status.is_available and row.active_p25_cents is not None:
            scarce.append(row)
    if not scarce:
        return ScarcityStats(days=0, resale_cents=None, sellers=0)
    return ScarcityStats(
        days=len(scarce),
        resale_cents=round(statistics.median(r.active_p25_cents for r in scarce)),
        sellers=round(statistics.median(r.active_count for r in scarce)),
    )
