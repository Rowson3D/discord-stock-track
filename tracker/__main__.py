"""Command-line entry point: `uv run tracker <command>` or `python -m tracker <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from tracker.app import run_tracker
from tracker.db.migrate import upgrade_async
from tracker.db.session import create_engine, create_sessionmaker
from tracker.domain import ListingSnapshot, format_cents
from tracker.http import HttpClient
from tracker.ingest import Ingestor
from tracker.legacy import import_legacy
from tracker.logconfig import configure_logging
from tracker.outbox import load_events
from tracker.settings import Settings, get_settings
from tracker.sources.unifi import UnifiStore

logger = logging.getLogger("tracker")


def _print_snapshots(snapshots: Sequence[ListingSnapshot]) -> None:
    for s in sorted(snapshots, key=lambda s: (s.category or "", s.sku)):
        flags = []
        if s.is_early_access:
            flags.append("EA")
        if s.restock_eta:
            flags.append(f"ETA {s.restock_eta.isoformat()}")
        if s.regular_price_cents:
            flags.append(f"was {format_cents(s.regular_price_cents, s.currency)}")
        print(
            f"  {s.sku:<24} {s.status.value:<13} {format_cents(s.price_cents, s.currency):>11}"
            f"  {s.category or '-':<28} {' · '.join(flags)}"
        )


async def cmd_db_upgrade(settings: Settings, _args: argparse.Namespace) -> int:
    await upgrade_async(settings.app.resolved_database_url)
    print(f"Database at head: {settings.app.resolved_database_url}")
    return 0


async def cmd_unifi_product(settings: Settings, args: argparse.Namespace) -> int:
    async with HttpClient(settings.http) as http:
        snapshots = await UnifiStore(http, settings.unifi).fetch_product(args.slug, args.region)
    print(f"{args.slug} ({args.region}): {len(snapshots)} variant(s)")
    _print_snapshots(snapshots)
    return 0


async def cmd_unifi_crawl(settings: Settings, args: argparse.Namespace) -> int:
    regions = [args.region] if args.region else settings.unifi.regions
    engine = None
    if args.save:
        await upgrade_async(settings.app.resolved_database_url)
        engine = create_engine(settings.app.resolved_database_url)

    exit_code = 0
    try:
        async with HttpClient(settings.http) as http:
            store = UnifiStore(http, settings.unifi)
            for region in regions:
                result = await store.crawl_catalog(region)
                statuses = Counter(s.status.value for s in result.snapshots)
                print(
                    f"\n[{region}] {len(result.snapshots)} listings across "
                    f"{len(result.categories_crawled)} categories: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
                )
                if result.errors:
                    exit_code = 1
                    for slug, error in result.errors.items():
                        print(f"  ! {slug}: {error}")

                unavailable = [s for s in result.snapshots if not s.status.is_available]
                if unavailable:
                    print("  Not available:")
                    _print_snapshots(unavailable)
                if args.verbose:
                    print("  All listings:")
                    _print_snapshots(result.snapshots)

                if engine is not None:
                    async with create_sessionmaker(engine)() as session, session.begin():
                        report = await Ingestor(session).ingest(result.snapshots)
                    print(
                        f"  Saved: {report.created_listings} new listings, "
                        f"{report.observations} changes, {report.unchanged} unchanged"
                    )
                    for event in report.events:
                        print(f"  • {event.kind}: {event.old_value} → {event.new_value}")
    finally:
        if engine is not None:
            await engine.dispose()
    return exit_code


async def cmd_import_legacy(settings: Settings, args: argparse.Namespace) -> int:
    data_dir = settings.app.data_dir
    watchlist = Path(args.watchlist) if args.watchlist else data_dir / "watchlist.json"
    subscribers = Path(args.subscribers) if args.subscribers else data_dir / "subscribers.json"
    if not watchlist.exists() and not subscribers.exists():
        print(f"Nothing to import: neither {watchlist} nor {subscribers} exists.")
        return 1

    await upgrade_async(settings.app.resolved_database_url)
    engine = create_engine(settings.app.resolved_database_url)
    try:
        async with HttpClient(settings.http) as http:
            unifi = None if args.no_resolve else UnifiStore(http, settings.unifi)
            async with create_sessionmaker(engine)() as session, session.begin():
                report = await import_legacy(
                    session, watchlist_path=watchlist, subscribers_path=subscribers, unifi=unifi
                )
    finally:
        await engine.dispose()

    print(
        f"Imported {report.entries} watchlist entries → {report.listings_created} new listings, "
        f"{report.watches_created} new watches."
    )
    if report.skipped_checkout_configs:
        print(
            f"Note: {report.skipped_checkout_configs} entries had checkout settings; "
            "those are migrated when guarded checkout is rebuilt (Phase 6)."
        )
    for item in report.unresolved:
        print(f"  ! unresolved: {item}")
    return 1 if report.unresolved else 0


async def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    await run_tracker(settings, regions=[args.region] if args.region else None)
    return 0


async def cmd_radar(settings: Settings, args: argparse.Namespace) -> int:
    await upgrade_async(settings.app.resolved_database_url)
    engine = create_engine(settings.app.resolved_database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            events = await load_events(
                session, pending_only=False, limit=args.limit, kinds=args.kind or None
            )
    finally:
        await engine.dispose()
    if not events:
        print("No radar events yet. Run `tracker run` or `tracker unifi crawl --save`.")
        return 0
    for event in reversed(events):
        print(f"{event.occurred_at:%Y-%m-%d %H:%M} UTC  {event.summary()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracker", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    commands = parser.add_subparsers(dest="command", required=True)

    db = commands.add_parser("db", help="database management").add_subparsers(
        dest="db_command", required=True
    )
    db.add_parser("upgrade", help="apply migrations").set_defaults(handler=cmd_db_upgrade)

    legacy = commands.add_parser("import-legacy", help="import the old bot's JSON data")
    legacy.add_argument("--watchlist", help="path to watchlist.json (default: data dir)")
    legacy.add_argument("--subscribers", help="path to subscribers.json (default: data dir)")
    legacy.add_argument(
        "--no-resolve", action="store_true", help="don't fetch UniFi pages (UniFi entries skipped)"
    )
    legacy.set_defaults(handler=cmd_import_legacy)

    unifi = commands.add_parser("unifi", help="Ubiquiti store tools").add_subparsers(
        dest="unifi_command", required=True
    )
    product = unifi.add_parser("product", help="fetch and parse one product page")
    product.add_argument("slug", help="product slug, e.g. u7-pro")
    product.add_argument("--region", default="us")
    product.set_defaults(handler=cmd_unifi_product)

    crawl = unifi.add_parser("crawl", help="crawl the full catalog")
    crawl.add_argument("--region", help="single region (default: UNIFI_STORE_REGIONS)")
    crawl.add_argument("--save", action="store_true", help="store results and emit radar events")
    crawl.set_defaults(handler=cmd_unifi_crawl)

    run = commands.add_parser("run", help="run the continuous tracker (Ctrl+C to stop)")
    run.add_argument("--region", help="single region (default: UNIFI_STORE_REGIONS)")
    run.set_defaults(handler=cmd_run)

    radar = commands.add_parser("radar", help="show recent radar events")
    radar.add_argument("--limit", type=int, default=30)
    radar.add_argument(
        "--kind", action="append", help="filter by kind (repeatable), e.g. --kind back_in_stock"
    )
    radar.set_defaults(handler=cmd_radar)
    return parser


def _utf8_output() -> None:
    """Windows pipes default to a legacy code page that can't encode arrows/bullets."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_output()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(
        "DEBUG" if args.verbose else settings.app.log_level,
        settings.app.log_file,
        settings.app.log_json,
    )
    try:
        return asyncio.run(args.handler(settings, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
