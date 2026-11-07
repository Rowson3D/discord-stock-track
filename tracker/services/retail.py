"""Polling for URL-based retailer listings (Amazon, Best Buy, B&H, Newegg)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Listing
from tracker.domain import ListingSnapshot, Priority, SourceKey
from tracker.http import FetchError
from tracker.ingest import Ingestor
from tracker.outbox import Dispatcher
from tracker.settings import RetailSettings
from tracker.sources.web import WEB_SOURCES, BlockedError, url_snapshot

logger = logging.getLogger(__name__)

PRIORITY_MULTIPLIERS = {
    Priority.HIGH.value: 0.5,
    Priority.NORMAL.value: 1.0,
    Priority.LOW.value: 1.5,
}

Fetcher = Callable[[str, SourceKey], Awaitable[ListingSnapshot]]


def effective_interval(settings: RetailSettings, listing: Listing) -> float:
    """Per-source interval × priority, backed off for listings that keep failing."""
    interval = settings.interval_for(listing.source) * PRIORITY_MULTIPLIERS.get(
        listing.priority, 1.0
    )
    failures = listing.consecutive_unknowns
    if failures >= 50:
        return min(interval * 20, 4 * 3600)
    if failures >= 10:
        return min(interval * 10, 3600)
    return interval


class RetailService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        fetch: Fetcher,
        settings: RetailSettings,
        dispatcher: Dispatcher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._fetch = fetch
        self._settings = settings
        self._dispatcher = dispatcher
        self._clock = clock

    async def due_listings(self) -> list[tuple[int, str, SourceKey]]:
        now = self._clock()
        async with self._sessions() as session:
            listings = (
                await session.scalars(
                    select(Listing).where(
                        Listing.source.in_([s.value for s in WEB_SOURCES]),
                        Listing.enabled.is_(True),
                        Listing.delisted_at.is_(None),
                    )
                )
            ).all()
        due = [
            listing
            for listing in listings
            if listing.last_checked_at is None
            or now - listing.last_checked_at
            >= timedelta(seconds=effective_interval(self._settings, listing))
        ]
        due.sort(key=lambda item: item.last_checked_at or datetime.min.replace(tzinfo=UTC))
        return [(item.id, item.url, SourceKey(item.source)) for item in due]

    async def poll_due(self) -> int:
        polled = 0
        for listing_id, url, source in (await self.due_listings())[: self._settings.max_per_tick]:
            await self.poll(listing_id, url, source)
            polled += 1
        if polled:
            await self._dispatcher.dispatch()
        return polled

    async def poll(self, listing_id: int, url: str, source: SourceKey) -> bool:
        now = self._clock()
        try:
            snapshot = await self._fetch(url, source)
        except (FetchError, BlockedError) as exc:
            async with self._sessions() as session, session.begin():
                listing = await session.get(Listing, listing_id)
                if listing is not None:
                    listing.last_checked_at = now
                    listing.consecutive_unknowns += 1
                    count = listing.consecutive_unknowns
                    if count <= 3 or count % 10 == 0:
                        logger.warning("%s failed %d time(s) in a row: %s", url, count, exc)
            return False

        async with self._sessions() as session, session.begin():
            await Ingestor(session).ingest([snapshot], observed_at=now)
        return True

    async def add_url(self, url: str, source: SourceKey) -> tuple[int, ListingSnapshot]:
        """Create (or find) a listing for a retailer URL. Blocked pages still create it."""
        try:
            snapshot = await self._fetch(url, source)
        except (FetchError, BlockedError) as exc:
            logger.info("Initial fetch of %s failed (%s); tracking it anyway", url, exc)
            snapshot = url_snapshot(url, source)
        async with self._sessions() as session, session.begin():
            await Ingestor(session).ingest([snapshot], observed_at=self._clock())
            listing_id = await session.scalar(
                select(Listing.id).where(
                    Listing.source == source.value,
                    Listing.region == snapshot.region,
                    Listing.external_id == snapshot.external_id,
                )
            )
        assert listing_id is not None
        return listing_id, snapshot
