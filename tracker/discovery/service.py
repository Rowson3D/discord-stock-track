"""Discovery pipeline: signals → candidates → eBay check → promising → your decision.

Statuses: `new` (not checked yet), `checked` (market read, not promising), `promising`
(meets the profit bar; rechecked daily during a trial), `tracked` (you chose it; checked
daily), `ignored` (you hid it; never checked again), `dropped` (faded; a new signal revives it).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Candidate, CandidateHistory, CandidateSignal
from tracker.discovery.identity import candidate_key, clean_title, pick_model
from tracker.discovery.market import decayed_heat, estimate, read_market, score
from tracker.domain import ListingSnapshot, SourceKey
from tracker.market.ebay import EbayClient, EbayError
from tracker.settings import DiscoverySettings, MarketSettings
from tracker.sources.web import detect_source

logger = logging.getLogger(__name__)

ACTIVE = ("new", "checked", "promising", "tracked")
# How much one signal says "this is hot".
WEIGHTS = {
    "manual": 2.0,
    "sold_out": 1.5,
    "big_discount": 1.2,
    "trending": 1.0,
    "most_viewed": 0.8,
}


@dataclass(frozen=True, slots=True)
class Signal:
    source: str
    kind: str
    title: str
    url: str | None = None
    price_cents: int | None = None
    regular_cents: int | None = None
    upc: str | None = None
    model: str | None = None
    brand: str | None = None
    category: str | None = None
    available: bool | None = None
    rank: int | None = None
    # Set when `url` is a page where the product can be bought (price/availability apply).
    retail_source: str | None = None

    @property
    def weight(self) -> float:
        base = WEIGHTS.get(self.kind, 0.5)
        if self.rank:
            base *= 1 + max(0, 10 - self.rank) / 10
        return base


class SignalSource(Protocol):
    name: str

    async def signals(self) -> list[Signal]: ...


FetchListing = Callable[[str, SourceKey], Awaitable[ListingSnapshot]]


class DiscoveryService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ebay: EbayClient | None,
        market: MarketSettings,
        settings: DiscoverySettings,
        *,
        fetch_listing: FetchListing | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._ebay = ebay
        self._market = market
        self._settings = settings
        self._fetch = fetch_listing
        self._clock = clock
        self._calls_day = ""
        self._calls = 0

    # ── budget ───────────────────────────────────────────────────────────

    @property
    def ebay_ready(self) -> bool:
        return self._ebay is not None

    def budget(self) -> tuple[int, int]:
        """(eBay calls used today, daily limit)."""
        today = self._clock().date().isoformat()
        if today != self._calls_day:
            self._calls_day, self._calls = today, 0
        return self._calls, self._settings.daily_ebay_calls

    def _spend(self) -> bool:
        used, limit = self.budget()
        if used >= limit:
            return False
        self._calls += 1
        return True

    # ── signals ──────────────────────────────────────────────────────────

    async def ingest(self, signals: Sequence[Signal]) -> int:
        """Add signals to candidates (creating them as needed). Returns new candidates."""
        now = self._clock()
        created = 0
        async with self._sessions() as session, session.begin():
            for signal in signals:
                if not signal.title.strip():
                    continue
                key = candidate_key(signal.title, model=signal.model, upc=signal.upc)
                candidate = await session.scalar(select(Candidate).where(Candidate.key == key))
                if candidate is None:
                    candidate = Candidate(
                        key=key,
                        title=clean_title(signal.title),
                        origin=signal.source,
                        status="new",
                        heat=0.0,
                        signal_count=0,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    session.add(candidate)
                    created += 1
                self._apply(candidate, signal, now)
                await session.flush()
                await self._record_signal(session, candidate, signal, now)
        return created

    def _apply(self, candidate: Candidate, signal: Signal, now: datetime) -> None:
        candidate.heat = decayed_heat(candidate.heat or 0.0, candidate.last_seen_at, now)
        candidate.heat += signal.weight
        candidate.last_seen_at = now
        candidate.signal_count = (candidate.signal_count or 0) + 1
        candidate.model = candidate.model or pick_model(signal.title, signal.model)
        candidate.upc = candidate.upc or signal.upc
        candidate.brand = candidate.brand or signal.brand
        candidate.category = candidate.category or signal.category
        if signal.retail_source and signal.price_cents:
            candidate.retail_cents = signal.price_cents
            candidate.regular_cents = signal.regular_cents
            candidate.retail_url = signal.url
            candidate.retail_source = signal.retail_source
            candidate.available = signal.available
        if candidate.status == "dropped":
            candidate.status = "new"  # A fresh signal revives a faded candidate.
            candidate.next_check_at = None
        elif candidate.status == "checked" and signal.weight >= 1.0:
            candidate.next_check_at = None  # Hot again: recheck soon.

    async def _record_signal(
        self, session: AsyncSession, candidate: Candidate, signal: Signal, now: datetime
    ) -> None:
        row = await session.scalar(
            select(CandidateSignal).where(
                CandidateSignal.candidate_id == candidate.id,
                CandidateSignal.source == signal.source,
                CandidateSignal.kind == signal.kind,
            )
        )
        if row is None:
            session.add(
                CandidateSignal(
                    candidate_id=candidate.id,
                    source=signal.source,
                    kind=signal.kind,
                    title=signal.title[:300],
                    url=(signal.url or None) and signal.url[:500],
                    price_cents=signal.price_cents,
                    rank=signal.rank,
                    times_seen=1,
                    first_seen_at=now,
                    seen_at=now,
                )
            )
        else:
            row.times_seen += 1
            row.seen_at = now
            row.rank = signal.rank
            row.price_cents = signal.price_cents or row.price_cents
            row.url = (signal.url or row.url or None) and (signal.url or row.url)[:500]

    async def collect(self, source: SignalSource) -> int:
        try:
            signals = await source.signals()
        except Exception:
            logger.exception("Discovery source %s failed", source.name)
            return 0
        created = await self.ingest(signals)
        logger.info(
            "Discovery %s: %d signal(s), %d new candidate(s)", source.name, len(signals), created
        )
        return created

    # ── checking ─────────────────────────────────────────────────────────

    async def _due_ids(self, session: AsyncSession, now: datetime, limit: int) -> list[int]:
        priority = case(
            (Candidate.status == "tracked", 0),
            (Candidate.status == "promising", 1),
            (Candidate.status == "new", 2),
            else_=3,
        )
        rows = await session.scalars(
            select(Candidate)
            .where(
                Candidate.status.in_(ACTIVE),
                (Candidate.next_check_at.is_(None)) | (Candidate.next_check_at <= now),
            )
            .order_by(priority, Candidate.heat.desc())
            .limit(limit * 5)
        )
        due = []
        for candidate in rows:
            heat = decayed_heat(candidate.heat or 0, candidate.last_seen_at, now)
            if candidate.status in ("new", "checked") and heat < self._settings.min_heat:
                continue  # Cooled off: not worth eBay calls until a new signal arrives.
            due.append(candidate.id)
            if len(due) >= limit:
                break
        return due

    async def check_due(self) -> int:
        if self._ebay is None:
            return 0
        now = self._clock()
        async with self._sessions() as session:
            ids = await self._due_ids(session, now, self._settings.checks_per_run)
        checked = 0
        for candidate_id in ids:
            if await self.check(candidate_id) is None:
                break  # Budget spent or eBay unavailable.
            checked += 1
        return checked

    async def check(self, candidate_id: int) -> Candidate | None:
        """Read the eBay market for one candidate now (costs one eBay call)."""
        if self._ebay is None or not self._spend():
            return None
        async with self._sessions() as session:
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                return None
            title, model, retail = candidate.title, candidate.model, candidate.retail_cents
        try:
            reading = await read_market(self._ebay, title, model=model, retail_cents=retail)
        except EbayError as error:
            logger.warning("Discovery eBay check failed for %s: %s", title, error)
            return None
        now = self._clock()
        async with self._sessions() as session, session.begin():
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                return None
            guess = estimate(
                reading,
                retail_cents=candidate.retail_cents,
                regular_cents=candidate.regular_cents,
                available=candidate.available,
                market=self._market,
                settings=self._settings,
            )
            candidate.ebay_query = reading.query[:200]
            candidate.ebay_sellers = reading.sellers
            candidate.ebay_listings = reading.listings
            candidate.ebay_p25_cents = reading.p25_cents
            candidate.ebay_median_cents = reading.median_cents
            candidate.ebay_low_cents = reading.low_cents
            candidate.resale_cents = guess.resale_cents
            candidate.net_cents = guess.net_cents
            candidate.roi = guess.roi
            candidate.confidence = guess.confidence
            candidate.capped = guess.capped
            heat = decayed_heat(candidate.heat or 0, candidate.last_seen_at, now)
            candidate.score = score(guess.net_cents, guess.roi, heat, reading.sellers)
            candidate.checked_at = now
            await self._snapshot(session, candidate, now)
            await self._advance(session, candidate, now)
            await session.flush()  # Save before detaching the result.
            session.expunge(candidate)
        return candidate

    async def _snapshot(self, session: AsyncSession, candidate: Candidate, now: datetime) -> None:
        row = await session.scalar(
            select(CandidateHistory).where(
                CandidateHistory.candidate_id == candidate.id,
                CandidateHistory.day == now.date(),
            )
        )
        if row is None:
            row = CandidateHistory(candidate_id=candidate.id, day=now.date())
            session.add(row)
        row.fetched_at = now
        row.sellers = candidate.ebay_sellers
        row.listings = candidate.ebay_listings
        row.p25_cents = candidate.ebay_p25_cents
        row.median_cents = candidate.ebay_median_cents
        row.retail_cents = candidate.retail_cents
        row.net_cents = candidate.net_cents

    def _promising_now(self, candidate: Candidate) -> bool:
        return (
            candidate.net_cents is not None
            and candidate.roi is not None
            and candidate.net_cents >= self._settings.min_profit_cents
            and candidate.roi >= self._settings.min_roi
            and candidate.ebay_sellers >= self._settings.min_sellers
        )

    async def _advance(self, session: AsyncSession, candidate: Candidate, now: datetime) -> None:
        daily = now + timedelta(days=1)
        if candidate.status == "tracked":
            candidate.next_check_at = daily
            return
        if candidate.status == "promising":
            trial_end = candidate.trial_until
            if trial_end is not None and trial_end.replace(tzinfo=trial_end.tzinfo or UTC) <= now:
                if await self._held_up(session, candidate):
                    candidate.next_check_at = now + timedelta(days=self._settings.recheck_days)
                else:
                    candidate.status = "dropped"
                    candidate.next_check_at = None
            else:
                candidate.next_check_at = daily
            return
        if self._promising_now(candidate):
            candidate.status = "promising"
            candidate.trial_until = now + timedelta(days=self._settings.trial_days)
            candidate.next_check_at = daily
            logger.info(
                "Discovery: promising %s (net %s, %d sellers)",
                candidate.title,
                candidate.net_cents,
                candidate.ebay_sellers,
            )
        else:
            candidate.status = "checked"
            candidate.next_check_at = now + timedelta(days=self._settings.recheck_days)

    async def _held_up(self, session: AsyncSession, candidate: Candidate) -> bool:
        """Did the estimated profit hold across the trial (median of daily snapshots)?"""
        nets = sorted(
            n
            for n in await session.scalars(
                select(CandidateHistory.net_cents).where(
                    CandidateHistory.candidate_id == candidate.id,
                    CandidateHistory.net_cents.is_not(None),
                )
            )
            if n is not None
        )
        if not nets:
            return False
        return nets[len(nets) // 2] >= self._settings.min_profit_cents

    # ── decisions and manual checks ──────────────────────────────────────

    async def decide(self, candidate_id: int, action: str) -> Candidate | None:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                return None
            if action == "track":
                candidate.status = "tracked"
                candidate.next_check_at = candidate.next_check_at or now
            elif action == "ignore":
                candidate.status = "ignored"
                candidate.next_check_at = None
            elif action == "restore":
                candidate.status = "new"
                candidate.next_check_at = None
            else:
                raise ValueError(f"unknown action {action!r}")
            candidate.decided_at = now
            await session.flush()  # Save before detaching the result.
            session.expunge(candidate)
        return candidate

    async def manual(self, text: str) -> tuple[int | None, str | None]:
        """'Check this item': a product name or store link → candidate, checked right away."""
        text = text.strip()
        if not text:
            return None, "Enter a product name or link."
        signal: Signal
        if text.lower().startswith(("http://", "https://")):
            source = detect_source(text)
            if source is None or source is SourceKey.UNIFI or self._fetch is None:
                return None, "Paste an Amazon, Best Buy, B&H, or Newegg link, or type a name."
            try:
                snap = await self._fetch(text, source)
            except Exception as error:  # Blocked page, network error, unparsable page.
                logger.warning("Discovery couldn't read %s: %s", text, error)
                return None, "Couldn't read that page. Try typing the product name instead."
            signal = Signal(
                source="manual",
                kind="manual",
                title=snap.product_name or text,
                url=text,
                price_cents=snap.price_cents,
                regular_cents=snap.regular_price_cents,
                brand=snap.brand,
                available=snap.status.is_available if snap.status.is_known else None,
                retail_source=source.value,
            )
        else:
            signal = Signal(source="manual", kind="manual", title=text[:300])
        await self.ingest([signal])
        key = candidate_key(signal.title, model=signal.model, upc=signal.upc)
        async with self._sessions() as session:
            candidate_id = await session.scalar(select(Candidate.id).where(Candidate.key == key))
        if candidate_id is None:
            return None, "Couldn't create a candidate."
        if self._ebay is None:
            return candidate_id, "Saved, but eBay isn't configured, so the market wasn't read."
        if await self.check(candidate_id) is None:
            return candidate_id, "Saved. Today's eBay budget is used up; it'll be checked later."
        return candidate_id, None
