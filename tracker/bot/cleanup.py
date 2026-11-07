"""Delete the bot's own old messages from its channels (opt-in, MESSAGE_CLEANUP_ENABLED)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

import discord

from tracker.settings import CleanupSettings

logger = logging.getLogger(__name__)


async def cleanup_messages(
    client: discord.Client,
    channel_ids: Iterable[int],
    settings: CleanupSettings,
    now: datetime,
) -> int:
    if client.user is None:
        return 0
    cutoff = now - timedelta(minutes=settings.ttl_minutes)
    deleted = 0
    for channel_id in dict.fromkeys(channel_ids):
        channel = client.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            continue
        async for message in channel.history(limit=settings.scan_limit, before=cutoff):
            if deleted >= settings.max_deletes_per_run:
                return deleted
            if message.pinned or message.author.id != client.user.id:
                continue
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                logger.debug("Could not delete message %s: %s", message.id, exc)
                continue
            deleted += 1
            await asyncio.sleep(settings.delete_delay_seconds)
    if deleted:
        logger.info("Message cleanup deleted %d old message(s)", deleted)
    return deleted
