"""Composition root for the long-running tracker (`tracker run`).

One asyncio loop runs the scheduler (UniFi crawls, feeds, hot polls, retailer polls,
outbox dispatch, message cleanup) and, when DISCORD_BOT_TOKEN is set, the Discord bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.alerts import AlertRouter
from tracker.db.migrate import upgrade_async
from tracker.db.session import create_engine, create_sessionmaker
from tracker.discovery.bestbuy import BestBuySource
from tracker.discovery.service import DiscoveryService
from tracker.domain import ListingSnapshot, SourceKey
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
from tracker.sources.bestbuy_api import BestBuyStock
from tracker.sources.unifi import UnifiStore
from tracker.sources.web import fetch_listing

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot
    from tracker.panel.server import PanelServer

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


def build_retail_fetch(
    settings: Settings, http: HttpClient
) -> tuple[Callable[[str, SourceKey], Awaitable[ListingSnapshot]], BestBuyStock | None]:
    """Page fetches for store links, except Best Buy through its API when a key is set."""
    pages = partial(fetch_listing, http)
    if not settings.bestbuy.configured:
        return pages, None
    stock = BestBuyStock(settings.bestbuy)
    settings.retail.bestbuy_interval_seconds = settings.bestbuy.stock_interval_seconds
    logger.info(
        "Best Buy stock checks use the official API (every %ss)",
        settings.bestbuy.stock_interval_seconds,
    )

    async def fetch(url: str, source: SourceKey) -> ListingSnapshot:
        if source is SourceKey.BESTBUY:
            return await stock.snapshot(url)
        return await pages(url, source)

    return fetch, stock


def build_discovery(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    ebay: EbayClient | None,
    http: HttpClient,
) -> tuple[DiscoveryService | None, BestBuySource | None]:
    if not settings.discovery.enabled:
        return None, None
    discovery = DiscoveryService(
        sessions,
        ebay,
        settings.market,
        settings.discovery,
        fetch_listing=partial(fetch_listing, http),
    )
    bestbuy = BestBuySource(settings.bestbuy) if settings.bestbuy.configured else None
    sources = ["check an item (panel)"] + (["Best Buy"] if bestbuy else [])
    logger.info(
        "Discovery on: %s; eBay budget %d/day%s",
        ", ".join(sources),
        settings.discovery.daily_ebay_calls,
        "" if ebay else " (eBay not configured: markets won't be read)",
    )
    return discovery, bestbuy


def add_discovery_jobs(
    scheduler: Scheduler,
    discovery: DiscoveryService,
    bestbuy: BestBuySource | None,
    settings: Settings,
) -> None:
    scheduler.add(
        "discovery:check",
        settings.discovery.check_interval_seconds,
        discovery.check_due,
        initial_delay=90.0,
    )
    if bestbuy is not None:
        scheduler.add(
            "discovery:bestbuy",
            settings.bestbuy.interval_minutes * 60,
            partial(discovery.collect, bestbuy),
            initial_delay=60.0,
        )


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
        logger.info("eBay not configured: valuations use manual comps only (Record a sale)")
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
    dispatcher = EventDispatcher(
        sessions,
        list(sinks or [LoggingSink()]),
        max_age=timedelta(minutes=settings.monitor.event_max_age_minutes),
    )

    try:
        async with HttpClient(settings.http) as http:
            store = UnifiStore(http, settings.unifi)
            unifi = UnifiService(sessions, store, settings.monitor, dispatcher)
            retail_fetch, bestbuy_stock = build_retail_fetch(settings, http)
            retail = RetailService(sessions, retail_fetch, settings.retail, dispatcher)
            scheduler = build_scheduler(unifi, retail, dispatcher, settings, regions)
            ebay = EbayClient(settings.ebay) if settings.ebay.configured else None
            market = MarketService(sessions, ebay, settings.market, dispatcher)
            add_market_jobs(scheduler, market, settings)
            discovery, bestbuy = build_discovery(settings, sessions, ebay, http)
            if discovery is not None:
                add_discovery_jobs(scheduler, discovery, bestbuy, settings)
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
                )
            else:
                logger.warning("DISCORD_BOT_TOKEN not set: running headless (events go to the log)")
            register_alert_sinks(settings, sessions, dispatcher, router, bot)

            panel = _build_panel(settings, sessions, scheduler, bot, unifi, retail, discovery)

            logger.info(
                "Tracker running: regions=%s, catalog every %ss, hot poll every %ss",
                ",".join(regions),
                settings.monitor.catalog_interval_seconds,
                settings.monitor.hot_tick_seconds,
            )
            try:
                await _run_until_stopped(scheduler, bot, settings, stop, panel)
            finally:
                if ebay is not None:
                    await ebay.aclose()
                if bestbuy is not None:
                    await bestbuy.aclose()
                if bestbuy_stock is not None:
                    await bestbuy_stock.aclose()
    finally:
        await engine.dispose()
        logger.info("Tracker stopped")


def register_alert_sinks(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    dispatcher: EventDispatcher,
    router: AlertRouter,
    bot: TrackerBot | None,
) -> list[str]:
    """Register every configured alert destination with the dispatcher.

    The dispatcher keeps its own list, so destinations must be added through it; appending
    to a list after it was handed over silently dropped every alert once.
    """
    cooldown = settings.discord.alert_cooldown_minutes
    if bot is not None:
        from tracker.bot.sink import DiscordSink

        dispatcher.add_sink(DiscordSink(bot, sessions, router, settings.discord))
    if settings.sms.configured:
        dispatcher.add_sink(SmsSink(settings.sms, sessions, router, cooldown_minutes=cooldown))
        logger.info("SMS alerts enabled for %d number(s)", len(settings.sms.to_numbers))
    if settings.ntfy.configured:
        from tracker.ntfy import NtfySink

        # The bot learns the owner from Discord when DISCORD_OWNER_IDS isn't set.
        owners = (
            (lambda: bot.owner_ids) if bot is not None else (lambda: settings.discord.owner_ids)
        )
        dispatcher.add_sink(NtfySink(settings.ntfy, sessions, owners, cooldown_minutes=cooldown))
        logger.info("ntfy phone push enabled (%s)", settings.ntfy.server)
    names = dispatcher.sink_names
    logger.info("Alerts go to: %s", ", ".join(names))
    return names


def _build_bot(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    unifi: UnifiService,
    retail: RetailService,
    market: MarketService,
    dispatcher: EventDispatcher,
    scheduler: Scheduler,
) -> TrackerBot:
    # Imported lazily so headless runs and tests don't need a Discord client.
    from tracker.bot.cleanup import cleanup_messages
    from tracker.bot.client import TrackerBot
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
    if settings.updates.configured:
        from tracker.updates import Updater

        bot.updater = Updater(settings.updates, settings.app.data_dir, bot.notify_update)
        if not bot.updater.version:
            logger.info(
                "Update DMs: this build doesn't know its commit (build with scripts/update.sh)"
            )
        scheduler.add(
            "updates:check",
            settings.updates.check_minutes * 60,
            bot.updater.check,
            initial_delay=45.0,
        )
    return bot


def _build_panel(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    scheduler: Scheduler,
    bot: TrackerBot | None,
    unifi: UnifiService | None = None,
    retail: RetailService | None = None,
    discovery: DiscoveryService | None = None,
) -> PanelServer | None:
    if not settings.panel.enabled:
        return None
    if not settings.panel.configured:
        logger.info("Owner panel off: run `tracker panel setup` to enable it")
        return None
    from tracker.panel.api import PanelDeps, create_app
    from tracker.panel.server import PanelServer
    from tracker.watching import Resolved, resolve_listings

    async def resolve(text: str, region: str | None) -> Resolved:
        return await resolve_listings(
            sessions,
            text,
            region,
            poll_unifi=unifi.poll_product if unifi is not None else None,
            add_retail_url=retail.add_url if retail is not None else None,
        )

    deps = PanelDeps(
        settings=settings,
        sessions=sessions,
        started_at=datetime.now(UTC),
        scheduler=scheduler,
        decide_guild=bot.apply_guild_decision if bot is not None else None,
        resolve=resolve,
        owner_ids=(lambda: bot.owner_ids) if bot is not None else None,
        discovery=discovery,
        alerts=bot,
    )
    try:
        app = create_app(deps)
    except ValueError as error:  # A bad panel key must never take the bot down.
        logger.error("Owner panel disabled: %s. Fix it with `tracker panel setup --rotate`.", error)
        return None
    panel = PanelServer(app, settings.panel)
    if not panel.bind():
        return None
    logger.info("Owner panel: %s (owner login with your authenticator code)", panel.url)
    return panel


async def _run_until_stopped(
    scheduler: Scheduler,
    bot: TrackerBot | None,
    settings: Settings,
    stop: asyncio.Event,
    panel: PanelServer | None = None,
) -> None:
    # The panel runs beside the core tasks but never decides when the tracker stops.
    panel_task = asyncio.create_task(panel.serve(), name="panel") if panel else None
    tasks = {asyncio.create_task(scheduler.run(stop), name="scheduler")}
    if bot is not None:
        token = settings.discord.bot_token.get_secret_value()  # type: ignore[union-attr]
        tasks.add(asyncio.create_task(bot.start(token), name="discord"))
    stop_waiter = asyncio.create_task(stop.wait(), name="stop")
    try:
        await asyncio.wait(tasks | {stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        if panel is not None and panel_task is not None:
            panel.stop()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(panel_task, timeout=5)
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
