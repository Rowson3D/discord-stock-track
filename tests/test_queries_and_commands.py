from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.app import LEGACY_MARKER, _run_until_stopped, auto_import_legacy
from tracker.bot.commands import TrackerCommands
from tracker.db.models import Watch
from tracker.domain import ListingSnapshot, StockStatus
from tracker.ingest import Ingestor
from tracker.queries import (
    add_watches,
    remove_watches,
    resolve_variant,
    search_variants,
    set_priority,
    set_subscribed,
    toggle_watch,
    tracker_stats,
    upcoming_restocks,
    user_watches,
    variant_listings,
)
from tracker.scheduler import Scheduler
from tracker.settings import Settings


def _snap(slug: str, region: str = "us", **overrides: object) -> ListingSnapshot:
    origin = "https://store.ui.com" if region == "us" else f"https://{region}.store.ui.com"
    values: dict[str, object] = {
        "region": region,
        "external_id": f"{slug.upper()}-US",
        "sku": slug.upper(),
        "url": f"{origin}/{region}/en/products/{slug}",
        "product_key": f"ubiquiti:{slug}",
        "product_name": f"Product {slug}",
    }
    values.update(overrides)
    return make_snapshot(**values)


@pytest.fixture
async def catalog(sessions: async_sessionmaker[AsyncSession]):
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                _snap("u7-pro"),
                _snap("u7-pro", region="ca", currency="CAD", price_cents=23900),
                _snap("u7-pro-max"),
                _snap(
                    "udm-se",
                    status=StockStatus.OUT_OF_STOCK,
                    restock_eta=date(2026, 11, 12),
                    product_name="Dream Machine SE",
                ),
            ],
            observed_at=T0,
        )
    return sessions


async def test_search_ranks_prefix_and_short_skus_first(catalog):
    async with catalog() as session:
        matches = await search_variants(session, "u7-pro")
        assert [m.sku for m in matches] == ["U7-PRO", "U7-PRO-MAX"]
        assert [m.sku for m in await search_variants(session, "dream")] == ["UDM-SE"]
        assert (await resolve_variant(session, "u7-pro")).sku == "U7-PRO"
        assert (await resolve_variant(session, "machine se")).sku == "UDM-SE"
        assert await resolve_variant(session, "nothing-like-this") is None


async def test_watch_lifecycle(catalog):
    async with catalog() as session, session.begin():
        variant = await resolve_variant(session, "U7-PRO")
        rows = await variant_listings(session, variant.id)
        assert [r.listing.region for r in rows] == ["ca", "us"]
        ids = [r.listing.id for r in rows]

        assert await add_watches(session, "7", ids, 20000) == 2
        assert await add_watches(session, "7", ids, 18000) == 0  # updates the cap
        mine = await user_watches(session, "7")
        assert {r.max_price_cents for r in mine} == {18000}

        assert await toggle_watch(session, "7", ids[0]) is False
        assert await toggle_watch(session, "7", ids[0]) is True
        assert await remove_watches(session, "7", ids) == 2

        assert await set_subscribed(session, "7", True) is True
        assert await set_subscribed(session, "7", True) is False
        assert await set_subscribed(session, "7", False) is True


async def test_restocks_stats_and_priority(catalog):
    async with catalog() as session, session.begin():
        restocks = await upcoming_restocks(session, T0.date(), days=7)
        assert [r.variant.sku for r in restocks] == ["UDM-SE"]
        assert await upcoming_restocks(session, T0.date(), days=2) == []

        stats = await tracker_stats(session, T0 + timedelta(hours=1))
        assert stats.status_counts[("us", "in_stock")] == 2
        assert stats.status_counts[("us", "out_of_stock")] == 1
        assert stats.last_checked["us"] == T0

        variant = await resolve_variant(session, "U7-PRO")
        assert await set_priority(session, variant.id, "high") == 2


# ── command resolution (no Discord connection needed) ─────────────────────


class FakeUnifi:
    def __init__(self, sessions) -> None:
        self.sessions = sessions
        self.polled: list[tuple[str, str]] = []

    async def poll_product(self, region: str, slug: str) -> bool:
        self.polled.append((region, slug))
        async with self.sessions() as session, session.begin():
            await Ingestor(session).ingest([_snap(slug, region=region)], observed_at=T0)
        return True


def _commands(sessions) -> tuple[TrackerCommands, FakeUnifi]:
    unifi = FakeUnifi(sessions)
    ctx = SimpleNamespace(sessions=sessions, unifi=unifi, retail=None)
    return TrackerCommands(SimpleNamespace(ctx=ctx)), unifi  # type: ignore[arg-type]


async def test_resolve_by_sku_with_region_filter(catalog):
    commands, _ = _commands(catalog)
    rows, error = await commands._resolve("U7-PRO", "ca")
    assert error is None
    assert [(r.listing.region, r.listing.currency) for r in rows] == [("ca", "CAD")]


async def test_resolve_unknown_unifi_url_fetches_page(catalog):
    commands, unifi = _commands(catalog)
    url = "https://store.ui.com/us/en/products/e7?variant=1"
    rows, error = await commands._resolve(url, None)
    assert error is None
    assert unifi.polled == [("us", "e7")]
    assert [r.variant.sku for r in rows] == ["E7"]

    rows, _ = await commands._resolve("https://store.ui.com/us/en/products/u7-pro", None)
    assert unifi.polled == [("us", "e7")]  # known page: no fetch
    assert [r.listing.region for r in rows] == ["us"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("https://example.com/thing", "supported store"),
        ("https://store.ui.com/us/en/category/all-wifi", "UniFi store product URL"),
        ("zzz-no-such-sku", "No product matches"),
    ],
)
async def test_resolve_errors(catalog, text: str, message: str):
    commands, _ = _commands(catalog)
    rows, error = await commands._resolve(text, None)
    assert rows == []
    assert message in error


# ── app wiring ────────────────────────────────────────────────────────────


class StubStore:
    async def fetch_product(self, slug: str, region: str = "us") -> list[ListingSnapshot]:
        return [_snap(slug, region=region)]


async def test_auto_import_legacy_runs_once(
    sessions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TRACKER_DATA_DIR", str(tmp_path))
    (tmp_path / "watchlist.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://store.ui.com/us/en/products/utr",
                    "site": "ui.com",
                    "watchers": ["42"],
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(env_file=None)
    await auto_import_legacy(settings, sessions, StubStore())  # type: ignore[arg-type]
    assert (tmp_path / LEGACY_MARKER).exists()

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Watch)) == 1
    (tmp_path / "subscribers.json").write_text('{"user_ids": ["9"]}', encoding="utf-8")
    await auto_import_legacy(settings, sessions, StubStore())  # type: ignore[arg-type]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Watch)) == 1


class CrashingBot:
    def __init__(self) -> None:
        self.closed = False

    async def start(self, token: str) -> None:
        raise RuntimeError("bad token")

    async def close(self) -> None:
        self.closed = True


async def test_bot_crash_stops_scheduler_and_propagates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    settings = Settings(env_file=None)
    scheduler = Scheduler()

    async def tick() -> None:
        return None

    scheduler.add("tick", 0.01, tick)
    bot = CrashingBot()
    stop = asyncio.Event()
    with pytest.raises(RuntimeError, match="bad token"):
        await asyncio.wait_for(_run_until_stopped(scheduler, bot, settings, stop), 2)  # type: ignore[arg-type]
    assert bot.closed
    assert stop.is_set()


async def test_stop_event_shuts_down_cleanly():
    settings = Settings(env_file=None)
    scheduler = Scheduler()
    stop = asyncio.Event()
    runner = asyncio.create_task(_run_until_stopped(scheduler, None, settings, stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(runner, 2)


async def test_slash_commands_register_and_serialize():
    """Builds the real command tree offline and serializes every command the way
    discord.py sends it to Discord (catches limits on names, descriptions, choices)."""
    from tracker.bot.client import TrackerBot

    settings = Settings(env_file=None)
    bot = TrackerBot(SimpleNamespace(settings=settings))  # type: ignore[arg-type]
    await bot.add_cog(TrackerCommands(bot))
    payloads = {cmd.name: cmd.to_dict(bot.tree) for cmd in bot.tree.get_commands()}
    assert set(payloads) == {
        "watch", "unwatch", "watches", "subscribe", "unsubscribe", "stock", "radar",
        "restocks", "status", "check", "priority", "testalert", "cleanup",
        "opportunities", "value", "comp", "forgetme", "about", "server", "scan",
    }  # fmt: skip
    for payload in payloads.values():
        assert 1 <= len(payload["description"]) <= 100
        for option in payload.get("options", []):
            assert len(option.get("choices", [])) <= 25
    watch_options = {o["name"]: o for o in payloads["watch"]["options"]}
    assert watch_options["product"]["autocomplete"] is True
    assert watch_options["product"]["required"] is True
    assert watch_options["max_price"]["required"] is False
    await bot.close()
