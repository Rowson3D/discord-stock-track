"""Alert policy: which channel an event goes to, who gets pinged, and what gets suppressed.

Pure decision logic plus two small queries (watches and recent events). Both the Discord
and SMS sinks use it, so they agree on what counts as an alert.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import Event, Watch, WatchRule
from tracker.domain import EventKind
from tracker.guilds import GuildConfig
from tracker.outbox import RadarEvent
from tracker.watching import is_price_drop, now_utc, rule_matches, watch_pings

__all__ = ["IMPORTANT_KINDS", "AlertPlan", "AlertRouter", "is_price_drop", "strip_valuation"]

# Events that go to the alert channel (with pings). Everything else is routine radar.
IMPORTANT_KINDS = frozenset(
    {
        EventKind.BACK_IN_STOCK,
        EventKind.EARLY_ACCESS,
        EventKind.NEW_LISTING,
        EventKind.RELISTED,
        EventKind.DEAL,
    }
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


def strip_valuation(event: RadarEvent) -> RadarEvent:
    """Remove eBay-derived numbers for servers that don't show valuations."""
    return dataclasses.replace(
        event,
        resale_cents=None,
        net_cents=None,
        roi=None,
        valuation_basis=None,
        valuation_confidence=None,
        comps_count=0,
    )


class AlertRouter:
    """Plans alerts for one destination (a Discord server, or the global SMS channel)."""

    async def plan(
        self,
        session: AsyncSession,
        events: Sequence[RadarEvent],
        guild: GuildConfig,
        *,
        home_guild_id: str | None = None,
        now: datetime | None = None,
    ) -> list[AlertPlan]:
        now = now or now_utc()
        listing_ids = {e.listing_id for e in events if e.listing_id is not None}
        watches = (
            await session.scalars(
                select(Watch).where(
                    or_(Watch.listing_id.is_(None), Watch.listing_id.in_(listing_ids))
                )
            )
        ).all()
        # Pings only reach people who set their watch in this server. Watches from before
        # multi-server support (guild_id NULL) belong to the home server.
        mine = [
            w
            for w in watches
            if (w.guild_id or home_guild_id) == guild.guild_id or guild.guild_id == "global"
        ]
        subscribers = sorted(
            {w.discord_user_id for w in mine if w.listing_id is None and not _muted_now(w, now)}
        )
        rules = [
            r
            for r in await session.scalars(select(WatchRule).where(WatchRule.enabled))
            if (r.guild_id or home_guild_id) == guild.guild_id or guild.guild_id == "global"
        ]
        by_listing: dict[int, list[Watch]] = {}
        for watch in mine:
            if watch.listing_id is not None:
                by_listing.setdefault(watch.listing_id, []).append(watch)

        plans: list[AlertPlan] = []
        for event in events:
            if not guild.show_valuations:
                event = strip_valuation(event)
            plan = self._plan_one(
                event, by_listing.get(event.listing_id or -1, []), subscribers, rules, guild, now
            )
            if event.kind is EventKind.DEAL and not guild.show_valuations:
                plan.suppressed = "valuations hidden in this server"
            # Checks the events table, so it also covers repeats within this batch.
            elif await self._recently_alerted(session, event, guild.cooldown_minutes):
                plan.suppressed = "cooldown"
            plans.append(plan)
        return plans

    @staticmethod
    def _plan_one(
        event: RadarEvent,
        watches: list[Watch],
        subscribers: list[str],
        rules: list[WatchRule],
        guild: GuildConfig,
        now: datetime,
    ) -> AlertPlan:
        mention_kinds = frozenset(EventKind(k) for k in guild.mention_kinds if k in EventKind)
        watchers = {w.discord_user_id for w in watches if watch_pings(w, event, mention_kinds, now)}
        ruled = {r.discord_user_id for r in rules if rule_matches(r, event, now)}
        # Anything someone explicitly asked to be pinged about belongs in the alert channel
        # (a watched price drop, a watch set to Sold out or Restock date, a rule match).
        important = event.kind in IMPORTANT_KINDS or bool(watchers) or bool(ruled)
        channel = guild.alert_channel_id if important else guild.effective_radar_channel
        plan = AlertPlan(event=event, important=important, channel_id=channel)
        mentioned = watchers | ruled
        if event.kind in mention_kinds:
            mentioned |= set(subscribers)
            plan.mention_text = guild.alert_mention.strip()
        plan.mention_user_ids = sorted(mentioned)
        return plan

    @staticmethod
    async def _recently_alerted(
        session: AsyncSession, event: RadarEvent, cooldown_minutes: float
    ) -> bool:
        cooldown = timedelta(minutes=cooldown_minutes)
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


def _muted_now(watch: Watch, now: datetime) -> bool:
    until = watch.muted_until
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=now.tzinfo)
    return until > now
