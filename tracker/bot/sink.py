"""Outbox sink that posts radar events to Discord."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Protocol

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.alerts import AlertPlan, AlertRouter
from tracker.bot.embeds import alert_content, alert_embed, digest_embed
from tracker.bot.views import alert_view
from tracker.outbox import RadarEvent
from tracker.settings import DiscordSettings

logger = logging.getLogger(__name__)

DIGEST_PAGE = 25


class ChannelSource(Protocol):
    """The slice of discord.Client the sink needs (lets tests use a fake)."""

    async def wait_until_ready(self) -> None: ...
    def get_channel(self, channel_id: int, /) -> Any: ...
    async def fetch_channel(self, channel_id: int, /) -> Any: ...


def allowed_mentions(plan: AlertPlan) -> discord.AllowedMentions:
    text = plan.mention_text
    return discord.AllowedMentions(
        users=True, roles=True, everyone="@everyone" in text or "@here" in text, replied_user=False
    )


class DiscordSink:
    name = "discord"

    def __init__(
        self,
        client: ChannelSource,
        sessions: async_sessionmaker[AsyncSession],
        router: AlertRouter,
        settings: DiscordSettings,
        *,
        ready_timeout: float = 60.0,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._router = router
        self._settings = settings
        self._ready_timeout = ready_timeout

    async def publish(self, events: Sequence[RadarEvent]) -> None:
        async with self._sessions() as session:
            plans = await self._router.plan(session, events)
        for plan in plans:
            if plan.suppressed:
                logger.info("Suppressed %s (%s)", plan.event.summary(), plan.suppressed)
        active = [p for p in plans if not p.suppressed and p.channel_id]
        if not active:
            return

        # Raising here leaves the events pending; the dispatcher retries later.
        await asyncio.wait_for(self._client.wait_until_ready(), timeout=self._ready_timeout)

        by_channel: dict[int, list[AlertPlan]] = {}
        for plan in active:
            assert plan.channel_id is not None
            by_channel.setdefault(plan.channel_id, []).append(plan)

        for channel_id, channel_plans in by_channel.items():
            channel = await self._channel(channel_id)
            if channel is None:
                continue
            individual = [p for p in channel_plans if p.important or p.pings]
            routine = [p for p in channel_plans if not (p.important or p.pings)]
            if len(routine) > self._settings.digest_threshold:
                for start in range(0, len(routine), DIGEST_PAGE):
                    page = [p.event for p in routine[start : start + DIGEST_PAGE]]
                    await self._send(channel, embed=digest_embed(page, limit=DIGEST_PAGE))
            else:
                individual.extend(routine)
            for plan in sorted(individual, key=lambda p: p.event.id):
                view = alert_view(plan.event.listing_id, plan.event.url)
                kwargs: dict[str, Any] = {
                    "content": alert_content(plan),
                    "embed": alert_embed(plan.event),
                    "allowed_mentions": allowed_mentions(plan),
                }
                if view is not None:
                    kwargs["view"] = view
                await self._send(channel, **kwargs)

    async def _channel(self, channel_id: int) -> Any:
        channel = self._client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self._client.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden) as exc:
            logger.error(
                "Alert channel %s unavailable (%s); check DISCORD_*_CHANNEL_ID", channel_id, exc
            )
            return None

    @staticmethod
    async def _send(channel: Any, **kwargs: Any) -> None:
        try:
            await channel.send(**kwargs)
        except (discord.Forbidden, discord.NotFound) as exc:
            # Permanent problems (missing permission, deleted channel): retrying won't help.
            logger.error("Cannot post to channel %s: %s", getattr(channel, "id", "?"), exc)
