"""A heartbeat file the container's health check reads.

Every few seconds, while the bot is connected to Discord and the database answers, the
scheduler touches a file. Docker's health check passes only while that file is fresh, so
"healthy" means the bot is really working, not just that the database file opens. It lives in
the temp folder (a tmpfs in the container), so a new container never sees the old one's file;
scripts/update.sh relies on that to tell whether an update came up.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "flipscout-heartbeat"
HEARTBEAT_SECONDS = 20.0
# docker-compose.yml's health check fails once the file is older than this.
MAX_AGE_SECONDS = 90


async def beat(
    sessions: async_sessionmaker[AsyncSession],
    ready: Callable[[], bool] | None = None,
    path: Path = HEARTBEAT_FILE,
) -> bool:
    """Touch the heartbeat file if the bot is up. Returns whether it did."""
    if ready is not None and not ready():
        logger.debug("No heartbeat: not connected to Discord")
        return False
    async with sessions() as session:
        await session.execute(text("select 1"))
    await asyncio.to_thread(path.touch)
    return True


def fresh(path: Path = HEARTBEAT_FILE, max_age: float = MAX_AGE_SECONDS) -> bool:
    try:
        return time.time() - path.stat().st_mtime < max_age
    except OSError:
        return False


__all__ = ["HEARTBEAT_FILE", "HEARTBEAT_SECONDS", "MAX_AGE_SECONDS", "beat", "fresh"]
