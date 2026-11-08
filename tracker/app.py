"""Composition root for the long-running tracker (`tracker run`).

One asyncio loop runs the scheduler (UniFi crawls, feeds, hot polls, retailer polls,
outbox dispatch, message cleanup) and, when DISCORD_BOT_TOKEN is set, the Discord bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.alerts import AlertRouter
from tracker.db.migrate import upgrade_async
from tracker.db.session import create_engine, create_sessionmaker
from tracker.guilds import bootstrap as bootstrap_guilds
from tracker.http import HttpClient
from tracker.instance import InstanceLock, SchemaGuard, code_schema_head, lock_path_for
from tracker.legacy import import_legacy
from tracker.market.ebay import EbayClient
from tracker.market.service import MarketService
from tracker.outbox import EventDispatcher, EventSink, LoggingSink
from tracker.scheduler import Scheduler
from tracker.services.retail import RetailService
from tracker.services.unifi import UnifiService
from tracker.settings import Settings
from tracker.sms import SmsSink
from tracker.sources.unifi import UnifiStore
from tracker.sources.web import fetch_listing

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot

logger = logging.getLogger(__name__)

DISPATCH_INTERVAL_SECONDS = 15.0
LEGACY_MARKER = ".legacy-imported"


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not supported on Windows; Ctrl+C still cancels via KeyboardInterrupt there.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


def build_scheduler(
    unifi: UnifiService,
    retail: RetailService,
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
            partial(unifi.crawl, region),
            initial_delay=index * 20.0,
        )
        scheduler.add(
            f"unifi:home:{region}",
            monitor.home_interval_seconds,
            partial(unifi.check_home, region),
            initial_delay=15.0 + index * 5.0,
        )
    scheduler.add("unifi:hot", monitor.hot_tick_seconds, unifi.poll_hot, initial_delay=30.0)
    scheduler.add("retail:poll", settings.retail.tick_seconds, retail.poll_due, initial_delay=10.0)
    # Safety net: retries events a failed sink left pending.
    scheduler.add("outbox:dispatch", DISPATCH_INTERVAL_SECONDS, dispatcher.dispatch, jitter=0)
    return scheduler


def add_market_jobs(scheduler: Scheduler, market: MarketService, settings: Settings) -> None:
    if market.collecting:
        scheduler.add(
            "market:collect",
            settings.market.collect_tick_seconds,
            market.collect_due,
            initial_delay=60.0,
        )
        logger.info("eBay comp collection enabled (%s)", settings.ebay.marketplace)
    else:
        logger.info("eBay not configured: valuations use manual comps only (/comp)")
    scheduler.add(
        "market:value",
        settings.market.valuation_interval_seconds,
        market.revalue_all,
        initial_delay=45.0,
    )


async def auto_import_legacy(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: UnifiStore
) -> None:
    """Import the old bot's JSON files once, so upgrading keeps everyone's watches."""
    data_dir = settings.app.data_dir
    watchlist, subscribers = data_dir / "watchlist.json", data_dir / "subscribers.json"
    marker = data_dir / LEGACY_MARKER

    def _pending() -> bool:
        return not marker.exists() and (watchlist.exists() or subscribers.exists())

    if not settings.app.auto_import_legacy or not await asyncio.to_thread(_pending):
        return
    async with sessions() as session, session.begin():
        report = await import_legacy(
            session, watchlist_path=watchlist, subscribers_path=subscribers, unifi=store
        )
    logger.info(
        "Imported legacy data: %d entries → %d listings, %d watches (%d unresolved)",
        report.entries,
        report.listings_created,
        report.watches_created,
        len(report.unresolved),
    )
    for item in report.unresolved:
        logger.warning("Legacy entry not imported: %s", item)
    await asyncio.to_thread(
        Path(marker).write_text,
        f"imported {datetime.now(UTC).isoformat()}: {report.entries} entries\n",
        "utf-8",
    )


async def run_tracker(
    settings: Settings,
    *,
    sinks: Sequence[EventSink] | None = None,
    regions: Sequence[str] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    url = settings.app.resolved_database_url
    # One tracker per database, taken before migrating so a second copy changes nothing.
    with InstanceLock(lock_path_for(url, settings.app.data_dir)) as lock:
        logger.info("Instance lock held: %s", lock.path)
        await _run_locked(settings, url, sinks=sinks, regions=regions, stop=stop)


async def _run_locked(
    settings: Settings,
    url: str,
    *,
    sinks: Sequence[EventSink] | None,
    regions: Sequence[str] | None,
    stop: asyncio.Event | None,
) -> None:
    stop = stop or asyncio.Event()
    _install_signal_handlers(stop)
    regions = list(regions or settings.unifi.regions)

    await upgrade_async(url)
    engine = create_engine(url)
    sessions = create_sessionmaker(engine)
    sink_list: list[EventSink] = list(sinks or [LoggingSink()])
    dispatcher = EventDispatcher(
        sessions, sink_list, max_age=timedelta(minutes=settings.monitor.event_max_age_minutes)
    )

    try:
        async with HttpClient(settings.http) as http:
            store = UnifiStore(http, settings.unifi)
            unifi = UnifiService(sessions, store, settings.monitor, dispatcher)
            retail = RetailService(
                sessions, partial(fetch_listing, http), settings.retail, dispatcher
            )
            scheduler = build_scheduler(unifi, retail, dispatcher, settings, regions)
            ebay = EbayClient(settings.ebay) if settings.ebay.configured else None
            market = MarketService(sessions, ebay, settings.market, dispatcher)
            add_market_jobs(scheduler, market, settings)
            guard = SchemaGuard(engine, await asyncio.to_thread(code_schema_head, url))

            async def schema_guard() -> None:
                if not await guard.check():
                    stop.set()

            scheduler.add("db:schema-guard", 60.0, schema_guard, initial_delay=60.0)
            await auto_import_legacy(settings, sessions, store)

            router = AlertRouter()
            async with sessions() as session, session.begin():
                approved = await bootstrap_guilds(session, settings.discord)
            if approved:
                logger.info("Pre-approved Discord server(s): %s", ", ".join(approved))
            if settings.sms.configured:
                sink_list.append(
                    SmsSink(
                        settings.sms,
                        sessions,
                        router,
                        cooldown_minutes=settings.discord.alert_cooldown_minutes,
                    )
                )
                logger.info("SMS alerts enabled for %d number(s)", len(settings.sms.to_numbers))

            bot = None
            if settings.discord.enabled:
                bot = _build_bot(
                    settings,
                    sessions,
                    unifi,
                    retail,
                    market,
                    dispatcher,
                    scheduler,
                    router,
                    sink_list,
                )
            else:
                logger.warning("DISCORD_BOT_TOKEN not set: running headless (events go to the log)")

            logger.info(
                "Tracker running: regions=%s, catalog every %ss, hot poll every %ss",
                ",".join(regions),
                settings.monitor.catalog_interval_seconds,
                settings.monitor.hot_tick_seconds,
            )
            try:
                await _run_until_stopped(scheduler, bot, settings, stop)
            finally:
                if ebay is not None:
                    await ebay.aclose()
    finally:
        await engine.dispose()
        logger.info("Tracker stopped")


def _build_bot(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    unifi: UnifiService,
    retail: RetailService,
    market: MarketService,
    dispatcher: EventDispatcher,
    scheduler: Scheduler,
    router: AlertRouter,
    sink_list: list[EventSink],
) -> TrackerBot:
    # Imported lazily so headless runs and tests don't need a Discord client.
    from tracker.bot.cleanup import cleanup_messages
    from tracker.bot.client import TrackerBot
    from tracker.bot.sink import DiscordSink
    from tracker.context import TrackerContext

    context = TrackerContext(
        settings=settings,
        sessions=sessions,
        unifi=unifi,
        retail=retail,
        market=market,
        dispatcher=dispatcher,
        scheduler=scheduler,
        started_at=datetime.now(UTC),
    )
    bot = TrackerBot(context)
    sink_list.append(DiscordSink(bot, sessions, router, settings.discord))
    if not settings.discord.channel_id:
        logger.warning("DISCORD_CHANNEL_ID not set: alerts won't be posted anywhere")
    if settings.cleanup.enabled:
        channels = [
            c for c in (settings.discord.channel_id, settings.discord.radar_channel_id) if c
        ]
        scheduler.add(
            "discord:cleanup",
            settings.cleanup.interval_minutes * 60,
            lambda: cleanup_messages(bot, channels, settings.cleanup, datetime.now(UTC)),
            initial_delay=120.0,
        )
    return bot


async def _run_until_stopped(
    scheduler: Scheduler, bot: TrackerBot | None, settings: Settings, stop: asyncio.Event
) -> None:
    tasks = {asyncio.create_task(scheduler.run(stop), name="scheduler")}
    if bot is not None:
        token = settings.discord.bot_token.get_secret_value()  # type: ignore[union-attr]
        tasks.add(asyncio.create_task(bot.start(token), name="discord"))
    stop_waiter = asyncio.create_task(stop.wait(), name="stop")
    try:
        await asyncio.wait(tasks | {stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        if bot is not None:
            await bot.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        stop_waiter.cancel()
    for result in results:
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            raise result
