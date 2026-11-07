"""Shared runtime objects handed to the Discord bot and other front ends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.market.service import MarketService
from tracker.outbox import EventDispatcher
from tracker.scheduler import Scheduler
from tracker.services.retail import RetailService
from tracker.services.unifi import UnifiService
from tracker.settings import Settings


@dataclass(slots=True)
class TrackerContext:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    unifi: UnifiService
    retail: RetailService
    market: MarketService
    dispatcher: EventDispatcher
    scheduler: Scheduler
    started_at: datetime
