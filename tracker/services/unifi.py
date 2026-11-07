"""Continuous UniFi tracking: catalog crawls, restock-feed triggers, and hot-listing polls.

Three cooperating loops (scheduled by `tracker.app`):

* `crawl(region)`: full catalog every few minutes. Baseline for everything; also infers
  delistings after a complete crawl.
* `check_home(region)`: the home page's freshly-stocked and what's-new feeds. Cheap (one
  request) and fast-moving, so it runs every minute and queues product pages to poll now.
* `poll_hot()`: product pages for listings that matter right now (watched, high priority,
  Early Access, coming soon, restock imminent, recently sold out or restocked). Product
  pages are the only place the Early Access flag appears.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Event, Listing, Watch
from tracker.domain import EventKind, ListingSnapshot, Priority, SourceKey, StockStatus
from tracker.http import FetchError
from tracker.ingest import Ingestor
from tracker.outbox import Dispatcher
from tracker.settings import MonitorSettings
from tracker.sources.unifi.parser import HomePage, UnifiParseError
from tracker.sources.unifi.store import CrawlResult

logger = logging.getLogger(__name__)

SOURCE = SourceKey.UNIFI.value
# A complete crawl returning fewer than this share of active listings is treated as suspect
# (store glitch, A/B layout) and never used to infer delistings.
MIN_CRAWL_COVERAGE = 0.5
# One crawl creating more new listings than this (or this share of the catalog) looks like a
# store-side change rather than real launches: those are recorded without alerts.
NEW_LISTING_FLOOD_MIN = 25
NEW_LISTING_FLOOD_SHARE = 0.2
# Same idea for delisting.
DELIST_FLOOD_MIN = 10
DELIST_FLOOD_SHARE = 0.2
# Event kinds that make a listing "hot" for a while afterwards.
_HOT_EVENT_KINDS = (
    EventKind.SOLD_OUT.value,
    EventKind.BACK_IN_STOCK.value,
    EventKind.RESTOCK_ETA.value,
    EventKind.NEW_LISTING.value,
    EventKind.RELISTED.value,
)
_SEEN_LIMIT = 5000


class Store(Protocol):
    async def crawl_catalog(self, region: str = "us") -> CrawlResult: ...
    async def fetch_home(self, region: str = "us") -> HomePage: ...
    async def fetch_product(self, slug: str, region: str = "us") -> list[ListingSnapshot]: ...


@dataclass(slots=True)
class CrawlStats:
    region: str
    listings: int
    created: int
    changed: int
    events: int
    delisted: int
    complete: bool


def slug_from_url(url: str) -> str | None:
    if "/products/" not in url:
        return None
    return url.split("/products/", 1)[1].split("?", 1)[0].strip("/") or None


class _SeenSet:
    """Bounded insertion-ordered set so long-running processes don't grow without limit."""

    def __init__(self, limit: int = _SEEN_LIMIT) -> None:
        self._items: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._limit = limit

    def add(self, key: tuple[str, ...]) -> bool:
        """True if the key is new."""
        if key in self._items:
            return False
        self._items[key] = None
        if len(self._items) > self._limit:
            self._items.popitem(last=False)
        return True


class UnifiService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        settings: MonitorSettings,
        dispatcher: Dispatcher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._settings = settings
        self._dispatcher = dispatcher
        self._clock = clock
        # region -> product slugs to poll on the next hot tick (insertion ordered).
        self._pending: dict[str, OrderedDict[str, None]] = {}
        self._seen_restocks = _SeenSet()
        self._seen_whats_new = _SeenSet()

    # ── queue ─────────────────────────────────────────────────────────────

    def queue_product(self, region: str, slug: str) -> None:
        self._pending.setdefault(region, OrderedDict())[slug] = None

    def pending_count(self) -> int:
        return sum(len(slugs) for slugs in self._pending.values())

    def _take_pending(self, limit: int) -> list[tuple[str, str]]:
        taken: list[tuple[str, str]] = []
        for region, slugs in self._pending.items():
            while slugs and len(taken) < limit:
                slug, _ = slugs.popitem(last=False)
                taken.append((region, slug))
        return taken

    # ── catalog crawl ─────────────────────────────────────────────────────

    async def crawl(self, region: str) -> CrawlStats:
        started = self._clock()
        result = await self._store.crawl_catalog(region)
        grace = timedelta(minutes=self._settings.delist_grace_minutes)

        async with self._sessions() as session, session.begin():
            active_before = await session.scalar(
                select(func.count()).where(
                    Listing.source == SOURCE,
                    Listing.region == region,
                    Listing.delisted_at.is_(None),
                )
            )
            active_before = active_before or 0
            ingestor = Ingestor(session)
            report = await ingestor.ingest(result.snapshots, observed_at=started)

            new_events = [e for e in report.events if e.kind == EventKind.NEW_LISTING]
            flood_limit = max(NEW_LISTING_FLOOD_MIN, int(NEW_LISTING_FLOOD_SHARE * active_before))
            if len(new_events) > flood_limit:
                logger.warning(
                    "[%s] %d new listings in one crawl (limit %d); recording without alerts",
                    region,
                    len(new_events),
                    flood_limit,
                )
                for event in new_events:
                    await session.delete(event)
                report.events = [e for e in report.events if e.kind != EventKind.NEW_LISTING]

            delisted: list[Event] = []
            if not result.complete:
                logger.warning(
                    "[%s] crawl incomplete (%d category errors); skipping delisting",
                    region,
                    len(result.errors),
                )
            elif len(result.snapshots) < MIN_CRAWL_COVERAGE * active_before:
                logger.warning(
                    "[%s] crawl returned %d listings vs %d active; skipping delisting",
                    region,
                    len(result.snapshots),
                    active_before,
                )
            elif await ingestor.is_baseline(SOURCE, region):
                # First complete crawl: from now on, unseen listings are genuinely new.
                await ingestor.mark_baseline_complete(SOURCE, region, started)
                logger.info("[%s] baseline complete: %d listings", region, len(result.snapshots))
            else:
                delisted = await ingestor.mark_delisted(
                    SOURCE,
                    region,
                    stale_before=started - grace,
                    now=started,
                    max_count=max(DELIST_FLOOD_MIN, int(DELIST_FLOOD_SHARE * active_before)),
                )
            new_slugs = [
                slug
                for event in report.events
                if event.kind == EventKind.NEW_LISTING
                and event.listing is not None
                and (slug := slug_from_url(event.listing.url))
            ]

        # New listings get a product-page poll: it's the only way to learn Early Access.
        for slug in new_slugs:
            self.queue_product(region, slug)

        stats = CrawlStats(
            region=region,
            listings=len(result.snapshots),
            created=report.created_listings,
            changed=report.observations,
            events=len(report.events) + len(delisted),
            delisted=len(delisted),
            complete=result.complete,
        )
        logger.info(
            "[%s] crawl: %d listings, %d new, %d changed, %d events, %d delisted",
            region,
            stats.listings,
            stats.created,
            stats.changed,
            stats.events,
            stats.delisted,
        )
        await self._dispatcher.dispatch()
        return stats

    # ── home page feeds ───────────────────────────────────────────────────

    async def check_home(self, region: str) -> int:
        """Queue product polls for new freshly-stocked entries and what's-new cards."""
        home = await self._store.fetch_home(region)
        queued = 0
        for item in home.freshly_stocked:
            stamp = item.stocked_at.isoformat() if item.stocked_at else ""
            if self._seen_restocks.add((region, item.regional_sku, stamp)):
                self.queue_product(region, item.product_slug)
                queued += 1
        for slug in home.whats_new_slugs:
            if self._seen_whats_new.add((region, slug)):
                self.queue_product(region, slug)
                queued += 1
        if queued:
            logger.info("[%s] home feeds queued %d product poll(s)", region, queued)
        return queued

    # ── hot listings ──────────────────────────────────────────────────────

    def _hot_condition(self, now: datetime) -> ColumnElement[bool]:
        recent = now - timedelta(hours=self._settings.hot_recent_change_hours)
        eta_horizon = now.date() + timedelta(days=self._settings.hot_restock_eta_days)
        return or_(
            Listing.priority == Priority.HIGH.value,
            Listing.is_early_access.is_(True),
            Listing.status == StockStatus.COMING_SOON.value,
            exists().where(Watch.listing_id == Listing.id),
            and_(
                Listing.status != StockStatus.IN_STOCK.value,
                Listing.restock_eta.is_not(None),
                Listing.restock_eta <= eta_horizon,
            ),
            exists().where(
                Event.listing_id == Listing.id,
                Event.occurred_at >= recent,
                Event.kind.in_(_HOT_EVENT_KINDS),
            ),
        )

    async def due_hot_products(self, limit: int) -> list[tuple[str, str]]:
        """(region, slug) pairs of hot listings not polled within the hot interval."""
        now = self._clock()
        cutoff = now - timedelta(seconds=self._settings.hot_poll_interval_seconds)
        async with self._sessions() as session:
            rows = await session.execute(
                select(Listing.region, Listing.url)
                .where(
                    Listing.source == SOURCE,
                    Listing.enabled.is_(True),
                    Listing.delisted_at.is_(None),
                    or_(Listing.last_checked_at.is_(None), Listing.last_checked_at < cutoff),
                    self._hot_condition(now),
                )
                .order_by(Listing.last_checked_at.asc().nulls_first(), Listing.id)
                .limit(limit * 4)  # several variants share one product page
            )
        targets: OrderedDict[tuple[str, str], None] = OrderedDict()
        for region, url in rows.all():
            slug = slug_from_url(url)
            if slug:
                targets[(region, slug)] = None
            if len(targets) >= limit:
                break
        return list(targets)

    async def poll_hot(self) -> int:
        """Poll queued products first, then due hot listings, within the per-tick budget."""
        budget = self._settings.hot_products_per_tick
        targets = self._take_pending(budget)
        queued = len(targets)
        if len(targets) < budget:
            for target in await self.due_hot_products(budget - len(targets)):
                if target not in targets:
                    targets.append(target)
        if not targets:
            return 0

        polled = 0
        for region, slug in targets:
            if await self.poll_product(region, slug):
                polled += 1
        logger.info(
            "Hot poll: %d/%d product page(s) ok (%d queued, %d due, %d still queued): %s",
            polled,
            len(targets),
            queued,
            len(targets) - queued,
            self.pending_count(),
            ", ".join(f"{region}/{slug}" for region, slug in targets),
        )
        await self._dispatcher.dispatch()
        return polled

    async def poll_product(self, region: str, slug: str) -> bool:
        try:
            snapshots = await self._store.fetch_product(slug, region)
        except (FetchError, UnifiParseError) as exc:
            logger.warning("[%s] product %s poll failed: %s", region, slug, exc)
            return False
        async with self._sessions() as session, session.begin():
            await Ingestor(session).ingest(snapshots, observed_at=self._clock())
        return True
