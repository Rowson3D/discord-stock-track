"""Command-line entry point: `uv run tracker <command>` or `python -m tracker <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from tracker.app import run_tracker
from tracker.db.migrate import upgrade_async
from tracker.db.models import EbayStats, EbayStatsHistory, Valuation, Variant
from tracker.db.session import create_engine, create_sessionmaker
from tracker.domain import ListingSnapshot, format_cents
from tracker.history import humanize, stock_history
from tracker.http import HttpClient
from tracker.ingest import Ingestor
from tracker.legacy import import_legacy
from tracker.logconfig import configure_logging
from tracker.market.ebay import EbayClient
from tracker.market.service import MarketService
from tracker.market.valuation import ValuationResult
from tracker.outbox import EventDispatcher, LoggingSink, load_events
from tracker.queries import opportunities, resolve_variant, variant_comps, variant_listings
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


@asynccontextmanager
async def _market(settings: Settings) -> AsyncIterator[tuple[MarketService, Any]]:
    """A MarketService over the configured DB (eBay client only when credentials exist)."""
    await upgrade_async(settings.app.resolved_database_url)
    engine = create_engine(settings.app.resolved_database_url)
    sessions = create_sessionmaker(engine)
    ebay = EbayClient(settings.ebay) if settings.ebay.configured else None
    dispatcher = EventDispatcher(sessions, [LoggingSink()])
    try:
        yield MarketService(sessions, ebay, settings.market, dispatcher), sessions
    finally:
        if ebay is not None:
            await ebay.aclose()
        await engine.dispose()


def _print_valuation(sku: str, v: ValuationResult | Valuation | None) -> None:
    if v is None or v.basis is None:
        print(f"{sku}: not enough comps for a valuation")
        return
    roi = f"{v.roi:.0%}" if v.roi is not None else "—"
    print(
        f"{sku}: buy {format_cents(v.retail_cents)} → resale {format_cents(v.resale_cents)} "
        f"({v.basis}, {v.confidence}) → net {format_cents(v.net_cents)} ({roi} ROI)\n"
        f"  fees {format_cents(v.fees_cents)} · shipping {format_cents(v.shipping_cents)} · "
        f"cost {format_cents(v.cost_cents)} · sold {v.sold_count} (30d {v.sold_30d}) · "
        f"active {v.active_count}"
    )
    if v.basis == "capped":
        print("  capped: in stock at the store, so asking prices can't exceed store price + tax")
    if getattr(v, "scarcity_days", 0):
        print(
            f"  sold-out market: ~{format_cents(v.scarcity_resale_cents)} over "
            f"{v.scarcity_days} day(s) → net {format_cents(v.scarcity_net_cents)}"
        )
    flip = getattr(v, "flip_basis", None)
    print(
        f"  credible flip: {format_cents(v.flip_net_cents)} ({flip}, {v.flip_confidence})"
        if flip
        else "  credible flip: none yet (needs sold data, your sales, or sold-out history)"
    )


async def _variant_or_none(sessions: Any, sku: str) -> Variant | None:
    async with sessions() as session:
        variant = await resolve_variant(session, sku)
    if variant is None:
        print(f"No product matches {sku!r}.")
    return variant


async def cmd_market_value(settings: Settings, args: argparse.Namespace) -> int:
    async with _market(settings) as (market, sessions):
        variant = await _variant_or_none(sessions, args.sku)
        if variant is None:
            return 1
        result = (await market.revalue([variant.id])).get(variant.id)
        _print_valuation(variant.sku, result)
        async with sessions() as session:
            stats = await session.get(EbayStats, variant.id)
            comps = await variant_comps(session, variant.id)
        if stats is not None:
            print(
                f"  eBay (aggregates from {stats.fetched_at:%Y-%m-%d %H:%M} UTC): "
                f"{stats.matched} matched, {stats.excluded} excluded. "
                "Listings aren't stored; run `tracker market collect` to see them live."
            )
        for comp in comps:
            print(
                f"  manual {format_cents(comp.price_cents):>10} x{comp.quantity} "
                f"{comp.condition:<9} {comp.sold_at:%Y-%m-%d} {comp.note or ''}"
            )
    return 0


async def cmd_market_collect(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.ebay.configured:
        print("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (production keyset) first.")
        return 1
    async with _market(settings) as (market, sessions):
        variant = await _variant_or_none(sessions, args.sku)
        if variant is None:
            return 1
        report = await market.collect(variant.id)
        print(
            f"{variant.sku}: query {report.query!r} → {report.fetched} results, "
            f"{report.matched} matched, {report.excluded} excluded (shown live, not stored)"
        )
        for item, match in report.items:
            kind = "sold  " if item.sold else "listed"
            verdict = f"ok x{match.quantity}" if match.matched else f"excluded: {match.reason}"
            print(f"  {kind} {format_cents(item.price_cents):>10}  {verdict:<34} {item.title[:70]}")
        _print_valuation(variant.sku, report.valuation)
    return 0


async def cmd_market_comp(settings: Settings, args: argparse.Namespace) -> int:
    async with _market(settings) as (market, sessions):
        variant = await _variant_or_none(sessions, args.sku)
        if variant is None:
            return 1
        result = await market.add_manual_comp(
            variant.id,
            round(args.price * 100),
            shipping_cents=round(args.shipping * 100) if args.shipping is not None else None,
            condition=args.condition,
            quantity=args.quantity,
            note=args.note,
        )
        print(f"Recorded {format_cents(round(args.price * 100))} comp for {variant.sku}.")
        _print_valuation(variant.sku, result)
    return 0


async def cmd_market_scan(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.ebay.configured:
        print("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (production keyset) first.")
        return 1
    async with _market(settings) as (market, _):
        report = await market.scan(limit=args.limit, min_retail_cents=round(args.min_retail * 100))
    print(
        f"Priced {report.priced} items ({len(report.valued)} valued, {report.calls} eBay calls); "
        "asking prices, one per seller; read-only."
    )
    print("\nTop flips:")
    for i in report.top(15):
        eta = f" ETA {i.restock_eta}" if i.restock_eta and not i.available else ""
        print(
            f"  {i.sku:<22} {i.status.value:<12} {format_cents(i.retail_cents):>10} -> "
            f"{format_cents(i.resale_cents):>10}  net {format_cents(i.net_cents):>9} "
            f"({i.roi:.0%})  {i.sellers} sellers/{i.listings} listings  {i.confidence}{eta}"
        )
    if report.breakouts:
        print("\nPack breakouts:")
        for b in sorted(report.breakouts, key=lambda b: -b.pack_net_cents):
            print(
                f"  {b.pack_sku:<22} {b.pack_size}x {format_cents(b.pack_cents):>10} "
                f"({b.status.value})  unit {format_cents(b.unit_cost_cents)} vs "
                f"~{format_cents(b.single_resale_cents)}  pack net "
                f"{format_cents(b.pack_net_cents)}  "
                f"break-even {format_cents(b.breakeven_unit_cents)}/unit"
            )
    if report.losers():
        print("\nLoses money at retail:")
        for i in report.losers(8):
            print(
                f"  {i.sku:<22} {format_cents(i.retail_cents):>10} -> "
                f"{format_cents(i.resale_cents):>10}  net {format_cents(i.net_cents)}"
            )
    for error in report.errors:
        print(f"  ! {error}")
    return 0


async def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    await upgrade_async(settings.app.resolved_database_url)
    engine = create_engine(settings.app.resolved_database_url)
    now = datetime.now(UTC)
    try:
        async with create_sessionmaker(engine)() as session:
            variant = await resolve_variant(session, args.sku)
            if variant is None:
                print(f"No product matches {args.sku!r}.")
                return 1
            print(f"{variant.sku}")
            for row in await variant_listings(session, variant.id):
                listing = row.listing
                history = await stock_history(session, listing.id)
                gone = history.out_of_stock_for(now)
                sellout = humanize(history.median_sellout)
                flags = []
                if listing.delisted_at:
                    flags.append(f"delisted {listing.delisted_at:%Y-%m-%d}")
                if listing.raw_status in ("RegionNotAvailable", "Unavailable"):
                    flags.append("withdrawn from region")
                print(
                    f"  {listing.source}/{listing.region:<3} {listing.status:<13} "
                    f"restocks {history.restocks}  median sell-out {sellout}  "
                    f"out of stock {humanize(gone) if gone else '-'}"
                    f"{'+' if history.unavailable_at_least and gone else ''}  {' '.join(flags)}"
                )
            rows = list(
                await session.scalars(
                    select(EbayStatsHistory)
                    .where(EbayStatsHistory.variant_id == variant.id)
                    .order_by(EbayStatsHistory.day)
                )
            )
    finally:
        await engine.dispose()
    if rows:
        print("  eBay daily aggregates (active market):")
        for r in rows[-30:]:
            print(
                f"    {r.day}  sellers {r.active_count:>3} ({r.active_listings} listings)  "
                f"floor {format_cents(r.active_p25_cents):>10}  median "
                f"{format_cents(r.active_median_cents):>10}  retail {format_cents(r.retail_cents)}"
            )
    else:
        print("  No eBay history yet (collected automatically; daily for end-of-life candidates).")
    return 0


async def cmd_opportunities(settings: Settings, args: argparse.Namespace) -> int:
    async with _market(settings) as (market, sessions):
        await market.revalue_all()
        async with sessions() as session:
            rows = await opportunities(
                session,
                limit=args.limit,
                min_profit_cents=round(args.min_profit * 100),
                available_only=args.available,
            )
    if not rows:
        print("No profitable opportunities yet. Add comps (`tracker market comp`) or set up eBay.")
        return 0
    for rank, row in enumerate(rows, start=1):
        v = row.valuation
        roi = f"{v.flip_roi:.0%}" if v.flip_roi is not None else "—"
        state = "in stock" if v.available else "unavailable"
        resale = v.scarcity_resale_cents if v.flip_basis == "scarcity" else v.resale_cents
        print(
            f"{rank:>2}. {row.variant.sku:<22} buy {format_cents(v.retail_cents):>10} → "
            f"{format_cents(resale):>10}  net {format_cents(v.flip_net_cents):>9} ({roi:>4})  "
            f"{v.flip_basis}/{v.flip_confidence}  {state}  score {v.score:.1f}"
        )
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

    market = commands.add_parser("market", help="resale comps and valuations").add_subparsers(
        dest="market_command", required=True
    )
    value = market.add_parser("value", help="show a product's valuation and comps")
    value.add_argument("sku")
    value.set_defaults(handler=cmd_market_value)
    collect = market.add_parser("collect", help="fetch eBay comps for a product now")
    collect.add_argument("sku")
    collect.set_defaults(handler=cmd_market_collect)
    comp = market.add_parser("comp", help="record a manual (sold) comp")
    comp.add_argument("sku")
    comp.add_argument("price", type=float, help="sale price in dollars")
    comp.add_argument("--shipping", type=float, help="shipping the buyer paid")
    comp.add_argument("--condition", default="new", choices=["new", "open_box", "used"])
    comp.add_argument("--quantity", type=int, default=1)
    comp.add_argument("--note")
    comp.set_defaults(handler=cmd_market_comp)
    scan = market.add_parser("scan", help="read-only live profit scan against eBay")
    scan.add_argument("--limit", type=int, default=25, help="items to price (1 eBay call each)")
    scan.add_argument("--min-retail", type=float, default=100.0, help="dollars")
    scan.set_defaults(handler=cmd_market_scan)

    hist = commands.add_parser("history", help="stock cycles and eBay trend for a product")
    hist.add_argument("sku")
    hist.set_defaults(handler=cmd_history)

    opps = commands.add_parser("opportunities", help="rank resale opportunities")
    opps.add_argument("--limit", type=int, default=20)
    opps.add_argument("--min-profit", type=float, default=0.0, help="dollars")
    opps.add_argument("--available", action="store_true", help="only items in stock now")
    opps.set_defaults(handler=cmd_opportunities)
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
        secrets=settings.secret_values(),
    )
    try:
        return asyncio.run(args.handler(settings, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
