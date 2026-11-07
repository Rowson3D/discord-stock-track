"""Alert policy: which channel an event goes to, who gets pinged, and what gets suppressed.

Pure decision logic plus two small queries (watches and recent events). Both the Discord
and SMS sinks use it, so they agree on what counts as an alert.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import Event, Watch
from tracker.domain import EventKind
from tracker.outbox import RadarEvent
from tracker.settings import DiscordSettings
from tracker.sources.web import parse_money

# Events that go to the alert channel (with pings). Everything else is routine radar.
IMPORTANT_KINDS = frozenset(
    {EventKind.BACK_IN_STOCK, EventKind.EARLY_ACCESS, EventKind.NEW_LISTING, EventKind.RELISTED}
)


@dataclass(slots=True)
class AlertPlan:
    event: RadarEvent
    important: bool
    channel_id: int | None
    mention_user_ids: list[str] = field(default_factory=list)
    mention_text: str = ""
    suppressed: str | None = None

    @property
    def mentions(self) -> str:
        users = " ".join(f"<@{user_id}>" for user_id in self.mention_user_ids)
        return " ".join(part for part in (users, self.mention_text) if part)

    @property
    def pings(self) -> bool:
        return bool(self.mention_user_ids or self.mention_text)


def is_price_drop(event: RadarEvent) -> bool:
    if event.kind is not EventKind.PRICE_CHANGE:
        return False
    old, new = parse_money(event.old_value), parse_money(event.new_value)
    return old is not None and new is not None and new < old


class AlertRouter:
    def __init__(self, settings: DiscordSettings) -> None:
        self._settings = settings
        self._mention_kinds = {EventKind(k) for k in settings.mention_kinds if k in EventKind}

    async def plan(self, session: AsyncSession, events: Sequence[RadarEvent]) -> list[AlertPlan]:
        listing_ids = {e.listing_id for e in events if e.listing_id is not None}
        watches = (
            await session.scalars(
                select(Watch).where(
                    or_(Watch.listing_id.is_(None), Watch.listing_id.in_(listing_ids))
                )
            )
        ).all()
        subscribers = sorted({w.discord_user_id for w in watches if w.listing_id is None})
        by_listing: dict[int, list[Watch]] = {}
        for watch in watches:
            if watch.listing_id is not None:
                by_listing.setdefault(watch.listing_id, []).append(watch)

        plans: list[AlertPlan] = []
        for event in events:
            plan = self._plan_one(event, by_listing.get(event.listing_id or -1, []), subscribers)
            # Checks the events table, so it also covers repeats within this batch.
            if await self._recently_alerted(session, event):
                plan.suppressed = "cooldown"
            plans.append(plan)
        return plans

    def _plan_one(
        self, event: RadarEvent, watches: list[Watch], subscribers: list[str]
    ) -> AlertPlan:
        drop = is_price_drop(event)
        important = event.kind in IMPORTANT_KINDS or (drop and bool(watches))
        channel = (
            self._settings.channel_id
            if important
            else self._settings.radar_channel_id or self._settings.channel_id
        )
        plan = AlertPlan(event=event, important=important, channel_id=channel)

        pings_watchers = event.kind in self._mention_kinds or drop
        if pings_watchers:
            plan.mention_user_ids = sorted(
                {
                    w.discord_user_id
                    for w in watches
                    if w.max_price_cents is None
                    or (event.price_cents is not None and event.price_cents <= w.max_price_cents)
                }
            )
        if event.kind in self._mention_kinds:
            plan.mention_user_ids = sorted(set(plan.mention_user_ids) | set(subscribers))
            plan.mention_text = self._settings.alert_mention.strip()
        return plan

    async def _recently_alerted(self, session: AsyncSession, event: RadarEvent) -> bool:
        cooldown = timedelta(minutes=self._settings.alert_cooldown_minutes)
        if not cooldown or event.listing_id is None:
            return False
        earlier = await session.scalar(
            select(Event.id)
            .where(
                Event.listing_id == event.listing_id,
                Event.kind == event.kind.value,
                Event.id < event.id,
                Event.occurred_at >= event.occurred_at - cooldown,
            )
            .limit(1)
        )
        return earlier is not None
