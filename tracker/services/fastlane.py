"""Drop detector: check a handful of chosen Ubiquiti US products every few seconds.

The normal hot poll gets to a watched product every few minutes. Products on the snatch list
(``Listing.snatch``) get their own lane instead:

- It asks for the store's page data (``/_next/data/<build>/us/en/products/<slug>.json``, about a
  fifth of the HTML page) with ``If-None-Match``. While nothing changed the store answers
  ``304 Not Modified`` with no body, so a check costs it next to nothing.
- Every 5 seconds when restocks are likely (weekdays 6am–6pm Pacific, within a day of the store's
  restock date, or for 10 minutes after any US restock, since they come in waves), every 20
  seconds otherwise. At most ``SNATCH_LIMIT`` products, so it stays under one request a second.
- A changed answer goes through the same parser, ingest and alerts as every other check.

One plain client with an honest user agent, no retries in a loop, no evasion: when the store
says no, the lane waits and tries again later.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Event, Listing
from tracker.domain import EventKind, ListingSnapshot, SourceKey
from tracker.ingest import Ingestor
from tracker.outbox import Dispatcher
from tracker.services.unifi import slug_from_url
from tracker.settings import UnifiSettings
from tracker.sources.unifi.parser import UnifiParseError, parse_product_page

logger = logging.getLogger(__name__)

SNATCH_LIMIT = 5
FAST_SECONDS = 5.0
SLOW_SECONDS = 20.0
WAVE = timedelta(minutes=10)
REGION = "us"
USER_AGENT = "FlipScout/1.0 (personal stock checker)"
_BUILD_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')
_QUIET_LOG_SECONDS = 300.0


def pacific() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Los_Angeles")
    except Exception:  # no time zone database (Windows without tzdata)
        return timezone(timedelta(hours=-8), "PST")


def busy_hours(now: datetime, tz: tzinfo | None = None) -> bool:
    """Weekdays 6am–6pm Pacific: when most US restocks have landed."""
    local = now.astimezone(tz or pacific())
    return local.weekday() < 5 and 6 <= local.hour < 18


@dataclass(frozen=True, slots=True)
class Target:
    slug: str
    restock_eta: date | None


@dataclass(slots=True)
class LaneStatus:
    """What Health and the product screen show."""

    items: int
    fast: bool
    reason: str


class FastLane:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: UnifiSettings,
        dispatcher: Dispatcher,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        tz: tzinfo | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._dispatcher = dispatcher
        self._clock = clock
        self._mono = monotonic
        self._tz = tz or pacific()
        self._client = httpx.AsyncClient(
            timeout=10,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._build: str | None = None
        self._etags: dict[str, str] = {}
        self._last_check: dict[str, float] = {}
        self._last_warning: dict[str, float] = {}
        self._wave_checked = -1e9
        self._wave_until: datetime | None = None
        self.checks = 0
        self.changes = 0

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def origin(self) -> str:
        return self._settings.origin(REGION)

    # ── what to check, and how often ────────────────────────────────────

    async def targets(self) -> list[Target]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Listing.url, Listing.restock_eta).where(
                    Listing.snatch.is_(True),
                    Listing.source == SourceKey.UNIFI.value,
                    Listing.region == REGION,
                    Listing.delisted_at.is_(None),
                )
            )
        found: dict[str, date | None] = {}
        for url, eta in rows.all():
            slug = slug_from_url(url)
            if slug and (slug not in found or (eta and (found[slug] is None or eta < found[slug]))):
                found[slug] = eta
        return [Target(slug, eta) for slug, eta in list(found.items())[:SNATCH_LIMIT]]

    async def _in_wave(self, now: datetime) -> bool:
        """A US restock in the last 10 minutes (checked every 15 seconds)."""
        mono = self._mono()
        if mono - self._wave_checked >= 15:
            self._wave_checked = mono
            async with self._sessions() as session:
                latest = await session.scalar(
                    select(func.max(Event.occurred_at))
                    .join(Listing, Event.listing_id == Listing.id)
                    .where(
                        Event.kind == EventKind.BACK_IN_STOCK.value,
                        Listing.region == REGION,
                    )
                )
            if latest is not None:
                latest = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
                self._wave_until = latest + WAVE
        return self._wave_until is not None and now < self._wave_until

    def why_fast(self, now: datetime, eta: date | None, wave: bool) -> str | None:
        if wave:
            return "US restocks happening now"
        if eta is not None and abs((eta - now.astimezone(self._tz).date()).days) <= 1:
            return "near its restock date"
        if busy_hours(now, self._tz):
            return "weekday restock hours"
        return None

    async def status(self) -> LaneStatus:
        targets = await self.targets()
        now = self._clock()
        wave = await self._in_wave(now) if targets else False
        reasons = [r for t in targets if (r := self.why_fast(now, t.restock_eta, wave))]
        if reasons:
            return LaneStatus(len(targets), True, reasons[0])
        return LaneStatus(len(targets), False, "outside restock hours")

    # ── the check ───────────────────────────────────────────────────────

    async def tick(self) -> int:
        """Check whatever is due. Returns how many products changed."""
        targets = await self.targets()
        if not targets:
            return 0
        now = self._clock()
        wave = await self._in_wave(now)
        changed: list[ListingSnapshot] = []
        for target in targets:
            fast = self.why_fast(now, target.restock_eta, wave)
            interval = FAST_SECONDS if fast else SLOW_SECONDS
            mono = self._mono()
            if mono - self._last_check.get(target.slug, -1e9) < interval:
                continue
            self._last_check[target.slug] = mono
            try:
                snapshots = await self.check(target.slug)
            except (httpx.HTTPError, UnifiParseError, ValueError) as exc:
                self._warn(target.slug, exc)
                continue
            if snapshots:
                changed.extend(snapshots)
        if changed:
            async with self._sessions() as session, session.begin():
                await Ingestor(session).ingest(changed, observed_at=self._clock())
            await self._dispatcher.dispatch()
        return len(changed)

    async def check(self, slug: str) -> list[ListingSnapshot] | None:
        """The product's snapshots when the store's data changed, None when it didn't."""
        if self._build is None:
            await self._refresh_build(slug)
        path = f"{REGION}/{self._settings.language}/products/{slug}.json"
        url = f"{self.origin}/_next/data/{self._build}/{path}"
        headers = {"If-None-Match": etag} if (etag := self._etags.get(slug)) else {}
        response = await self._client.get(url, headers=headers)
        self.checks += 1
        if response.status_code == 304:
            return None
        if response.status_code == 404:
            # The store shipped a new version of its site: new build id, same products.
            self._build = None
            self._etags.clear()
            return None
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        page = {
            "props": {"pageProps": data.get("pageProps") or {}},
            "query": {"store": REGION, "language": self._settings.language},
        }
        snapshots = parse_product_page(page, base_url=self.origin)
        if etag := response.headers.get("etag"):
            self._etags[slug] = etag
        self.changes += 1
        return snapshots

    async def _refresh_build(self, slug: str) -> None:
        response = await self._client.get(
            f"{self.origin}/{REGION}/{self._settings.language}/products/{slug}",
            headers={"Accept": "text/html"},
        )
        response.raise_for_status()
        match = _BUILD_RE.search(response.text)
        if not match:
            raise UnifiParseError("no buildId on the product page")
        self._build = match.group(1)
        logger.info("Drop detector: store build %s", self._build)

    def _warn(self, slug: str, exc: Exception) -> None:
        mono = self._mono()
        if mono - self._last_warning.get(slug, -1e9) >= _QUIET_LOG_SECONDS:
            self._last_warning[slug] = mono
            logger.warning("Drop detector: %s check failed: %s", slug, exc)


__all__ = [
    "FAST_SECONDS",
    "SLOW_SECONDS",
    "SNATCH_LIMIT",
    "FastLane",
    "LaneStatus",
    "busy_hours",
]
