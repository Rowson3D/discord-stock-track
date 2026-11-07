"""Composition root for the long-running tracker (`tracker run`)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence
from datetime import timedelta
from functools import partial

from tracker.db.migrate import upgrade_async
from tracker.db.session import create_engine, create_sessionmaker
from tracker.http import HttpClient
from tracker.outbox import EventDispatcher, EventSink, LoggingSink
from tracker.scheduler import Scheduler
from tracker.services.unifi import UnifiService
from tracker.settings import Settings
from tracker.sources.unifi import UnifiStore

logger = logging.getLogger(__name__)

DISPATCH_INTERVAL_SECONDS = 15.0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not supported on Windows; Ctrl+C still cancels via KeyboardInterrupt there.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


def build_scheduler(
    service: UnifiService,
    dispatcher: EventDispatcher,
    settings: Settings,
    regions: Sequence[str],
) -> Scheduler:
    monitor = settings.monitor
    scheduler = Scheduler()
    for index, region in enumerate(regions):
        # Stagger regions so their crawls don't overlap at startup.
        scheduler.add(
            f"unifi:catalog:{region}",
            monitor.catalog_interval_seconds,
            partial(service.crawl, region),
            initial_delay=index * 20.0,
        )
        scheduler.add(
            f"unifi:home:{region}",
            monitor.home_interval_seconds,
            partial(service.check_home, region),
            initial_delay=15.0 + index * 5.0,
        )
    scheduler.add("unifi:hot", monitor.hot_tick_seconds, service.poll_hot, initial_delay=30.0)
    # Safety net: retries events a failed sink left pending.
    scheduler.add("outbox:dispatch", DISPATCH_INTERVAL_SECONDS, dispatcher.dispatch, jitter=0)
    return scheduler


async def run_tracker(
    settings: Settings,
    *,
    sinks: Sequence[EventSink] | None = None,
    regions: Sequence[str] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    stop = stop or asyncio.Event()
    _install_signal_handlers(stop)
    regions = list(regions or settings.unifi.regions)
    url = settings.app.resolved_database_url

    await upgrade_async(url)
    engine = create_engine(url)
    sessions = create_sessionmaker(engine)
    dispatcher = EventDispatcher(
        sessions,
        sinks or [LoggingSink()],
        max_age=timedelta(minutes=settings.monitor.event_max_age_minutes),
    )
    try:
        async with HttpClient(settings.http) as http:
            store = UnifiStore(http, settings.unifi)
            service = UnifiService(sessions, store, settings.monitor, dispatcher)
            scheduler = build_scheduler(service, dispatcher, settings, regions)
            logger.info(
                "Tracker running: regions=%s, catalog every %ss, hot poll every %ss",
                ",".join(regions),
                settings.monitor.catalog_interval_seconds,
                settings.monitor.hot_tick_seconds,
            )
            await scheduler.run(stop)
    finally:
        await engine.dispose()
        logger.info("Tracker stopped")
