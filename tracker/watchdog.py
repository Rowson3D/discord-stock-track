"""Tell the owner when tracking breaks, instead of failing quietly.

A restock tracker's worst failure is silence: the store changes its pages, Discord goes away,
or a job keeps failing, and the owner only finds out after missing a drop. Every minute this
checks the things that have to keep moving and pushes a message (ntfy and a Discord DM, so
either one alone gets through) when something breaks and again when it's fixed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Event, Listing
from tracker.domain import SourceKey
from tracker.outbox import EventDispatcher
from tracker.scheduler import Scheduler

logger = logging.getLogger(__name__)

Notify = Callable[[str, str], Awaitable[None]]  # (title, text)

# Jobs whose repeated failure means missed restocks or missed alerts.
CRITICAL_JOBS = ("unifi:", "outbox:dispatch", "retail:poll")
JOB_FAILURES = 5
STUCK_ALERTS = timedelta(minutes=5)
FAILING_DESTINATION = timedelta(minutes=5)
SNATCH_FAILURES = 12  # about a minute of failed fast checks


class Watchdog:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        scheduler: Scheduler,
        dispatcher: EventDispatcher,
        notify: Notify,
        *,
        regions: Collection[str],
        crawl_interval_seconds: float,
        fastlane: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._notify = notify
        self._regions = list(regions)
        # Three missed crawls before it counts as broken (and never less than 15 minutes).
        self._stale_after = timedelta(seconds=max(900.0, crawl_interval_seconds * 3))
        self._fastlane = fastlane
        self._clock = clock
        self._open: dict[str, str] = {}
        self._failing_since: dict[str, datetime] = {}
        self._expired_seen = 0

    async def check(self) -> None:
        now = self._clock()
        problems = await self.problems(now)
        new = {key: text for key, text in problems.items() if key not in self._open}
        fixed = [self._open[key] for key in self._open if key not in problems]
        self._open = problems
        if new:
            await self._send(
                "⚠️ FlipScout needs attention", "\n".join(f"• {t}" for t in new.values())
            )
        if fixed:
            await self._send(
                "✅ FlipScout is back to normal", "\n".join(f"• Fixed: {t}" for t in fixed)
            )
        expired = self._dispatcher.expired - self._expired_seen
        if expired > 0:
            self._expired_seen = self._dispatcher.expired
            await self._send(
                "⚠️ FlipScout dropped alerts",
                f"{expired} alert(s) couldn't be delivered for over an hour and were given up on. "
                "Check Radar for what you missed.",
            )

    async def problems(self, now: datetime) -> dict[str, str]:
        found: dict[str, str] = {}
        async with self._sessions() as session:
            newest = await session.scalar(
                select(func.max(Listing.last_checked_at)).where(
                    Listing.source == SourceKey.UNIFI.value,
                    Listing.region.in_(self._regions),
                )
            )
            stuck = await session.scalar(
                select(func.count()).where(
                    Event.notified_at.is_(None), Event.occurred_at < now - STUCK_ALERTS
                )
            )
        if newest is not None:
            newest = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
            if now - newest > self._stale_after:
                found["crawl"] = (
                    f"The Ubiquiti store hasn't been checked for {_ago(now - newest)}. "
                    "Restocks aren't being seen."
                )
        if stuck:
            found["alerts"] = f"{stuck} alert(s) have been waiting over 5 minutes to go out."

        for sink, error in self._dispatcher.failing.items():
            since = self._failing_since.setdefault(sink, now)
            if now - since >= FAILING_DESTINATION:
                found[f"sink:{sink}"] = f"Alerts can't reach {sink}: {error}"
        for sink in list(self._failing_since):
            if sink not in self._dispatcher.failing:
                del self._failing_since[sink]

        for job in self._scheduler.jobs.values():
            if job.name.startswith(CRITICAL_JOBS) and job.failures >= JOB_FAILURES:
                found[f"job:{job.name}"] = f"`{job.name}` has failed {job.failures} times in a row."

        failures = getattr(self._fastlane, "failures", 0)
        if failures >= SNATCH_FAILURES:
            found["snatch"] = (
                f"The drop detector's checks have failed {failures} times in a row: "
                "snatch-list products aren't being watched closely."
            )
        return found

    async def _send(self, title: str, text: str) -> None:
        logger.warning("%s: %s", title, text.replace("\n", " "))
        try:
            await self._notify(title, text)
        except Exception:
            logger.exception("Couldn't tell the owner: %s", title)


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


__all__ = ["Watchdog"]
