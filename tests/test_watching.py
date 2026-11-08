"""Watch preferences (alert types, target price, mute), rules, and the /watches manager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from tests.conftest import T0
from tests.test_alerts import _history, _pending, discord_settings
from tracker.alerts import AlertRouter
from tracker.bot.views import WatchManager, watch_line, watches_embed
from tracker.db.models import Listing, Watch, WatchRule
from tracker.domain import EventKind, StockStatus
from tracker.guilds import GuildConfig
from tracker.watching import (
    alert_key,
    format_kinds,
    parse_kinds,
    rule_matches,
    user_watch_rows,
    watch_pings,
)

LATER = T0 + timedelta(hours=1)
SERVER_KINDS = frozenset({EventKind.BACK_IN_STOCK, EventKind.EARLY_ACCESS, EventKind.DEAL})


def event(kind: EventKind, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "kind": kind,
        "old_value": None,
        "new_value": None,
        "price_cents": 18900,
        "sku": "U7-Pro",
        "product_name": "Access Point U7 Pro",
        "category": "wifi-flagship",
        "region": "us",
        "net_cents": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def watch(**overrides: Any) -> Watch:
    values: dict[str, Any] = {"discord_user_id": "1", "listing_id": 1}
    values.update(overrides)
    return Watch(**values)


def rule(**overrides: Any) -> WatchRule:
    values: dict[str, Any] = {"discord_user_id": "9", "name": "r", "enabled": True}
    values.update(overrides)
    return WatchRule(**values)


# ── alert types ──────────────────────────────────────────────────────────


def test_alert_keys():
    drop = event(EventKind.PRICE_CHANGE, old_value="$189.00", new_value="$159.00")
    rise = event(EventKind.PRICE_CHANGE, old_value="$159.00", new_value="$189.00")
    assert alert_key(drop) == "price_drop"
    assert alert_key(rise) is None
    assert alert_key(event(EventKind.RELISTED)) == "back_in_stock"
    assert alert_key(event(EventKind.DELISTED)) is None
    assert format_kinds(["deal", "price_drop", "deal"]) == "deal,price_drop"
    assert format_kinds([]) == ""
    assert parse_kinds("") == frozenset()
    assert parse_kinds(None) is None
    with pytest.raises(ValueError, match="unknown"):
        format_kinds(["nope"])


def test_watch_defaults_follow_the_server():
    w = watch()
    assert watch_pings(w, event(EventKind.BACK_IN_STOCK), SERVER_KINDS, T0)
    assert not watch_pings(w, event(EventKind.SOLD_OUT), SERVER_KINDS, T0)
    drop = event(EventKind.PRICE_CHANGE, old_value="$189.00", new_value="$159.00")
    assert watch_pings(w, drop, SERVER_KINDS, T0)  # drops on your own watch always count


def test_watch_custom_types_target_and_mute():
    only_drops = watch(kinds="price_drop")
    assert not watch_pings(only_drops, event(EventKind.BACK_IN_STOCK), SERVER_KINDS, T0)
    drop = event(
        EventKind.PRICE_CHANGE, old_value="$189.00", new_value="$159.00", price_cents=15900
    )
    assert watch_pings(only_drops, drop, SERVER_KINDS, T0)
    sold_out_too = watch(kinds="sold_out")
    assert watch_pings(sold_out_too, event(EventKind.SOLD_OUT), SERVER_KINDS, T0)
    nothing = watch(kinds="")
    assert not watch_pings(nothing, event(EventKind.BACK_IN_STOCK), SERVER_KINDS, T0)

    capped = watch(max_price_cents=15000)
    assert not watch_pings(capped, event(EventKind.BACK_IN_STOCK), SERVER_KINDS, T0)
    assert watch_pings(capped, event(EventKind.BACK_IN_STOCK, price_cents=14900), SERVER_KINDS, T0)

    muted = watch(muted_until=LATER)
    assert not watch_pings(muted, event(EventKind.BACK_IN_STOCK), SERVER_KINDS, T0)
    assert watch_pings(muted, event(EventKind.BACK_IN_STOCK), SERVER_KINDS, LATER)  # expired


def test_rule_criteria():
    back = event(EventKind.BACK_IN_STOCK)
    assert rule_matches(rule(query="u7"), back, T0)
    assert rule_matches(rule(query="access point"), back, T0)  # name, case-insensitive
    assert not rule_matches(rule(query="g6"), back, T0)
    assert rule_matches(rule(category="wifi"), back, T0)
    assert not rule_matches(rule(category="camera"), back, T0)
    assert rule_matches(rule(regions="ca,us"), back, T0)
    assert not rule_matches(rule(regions="uk"), back, T0)
    assert not rule_matches(rule(max_price_cents=10000), back, T0)
    assert not rule_matches(rule(query="u7", enabled=False), back, T0)
    assert not rule_matches(rule(query="u7", muted_until=LATER), back, T0)
    assert not rule_matches(rule(query="u7"), event(EventKind.SOLD_OUT), T0)  # not a default
    assert rule_matches(rule(query="u7", kinds="sold_out"), event(EventKind.SOLD_OUT), T0)
    # Minimum profit only counts credible (flip) profit carried on the alert.
    assert not rule_matches(rule(min_profit_cents=3000), back, T0)
    assert rule_matches(rule(min_profit_cents=3000), event(EventKind.DEAL, net_cents=4000), T0)


# ── routing ──────────────────────────────────────────────────────────────


async def _plan(sessions, now=T0):
    async with sessions() as session:
        return await AlertRouter().plan(
            session,
            await _pending(sessions),
            GuildConfig.from_settings(discord_settings()),
            now=now,
        )


async def _add(sessions, *rows):
    async with sessions() as session, session.begin():
        listing_id = await session.scalar(select(Listing.id))
        for row in rows:
            if isinstance(row, Watch) and row.listing_id == -1:
                row.listing_id = listing_id
            session.add(row)


async def test_router_applies_watch_preferences(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    await _add(
        sessions,
        Watch(discord_user_id="drops-only", listing_id=-1, kinds="price_drop"),
        Watch(discord_user_id="muted", listing_id=-1, muted_until=LATER),
        Watch(discord_user_id="normal", listing_id=-1),
        Watch(discord_user_id="muted-sub", listing_id=None, muted_until=LATER),
    )
    _, back = await _plan(sessions)
    assert back.event.kind is EventKind.BACK_IN_STOCK
    assert back.mention_user_ids == ["normal"]
    _, back_later = await _plan(sessions, now=LATER + timedelta(minutes=1))
    assert back_later.mention_user_ids == ["muted", "muted-sub", "normal"]


async def test_rules_ping_and_promote_to_alert_channel(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK})  # a sold-out event
    await _add(
        sessions,
        WatchRule(discord_user_id="r1", name="U7 sellouts", query="u7", kinds="sold_out"),
        WatchRule(discord_user_id="r2", name="cameras", category="camera", kinds="sold_out"),
        WatchRule(discord_user_id="r3", name="off", query="u7", kinds="sold_out", enabled=False),
    )
    [sold_out] = await _plan(sessions)
    assert sold_out.mention_user_ids == ["r1"]
    assert sold_out.important  # someone asked for it, so it goes to the alert channel
    assert sold_out.channel_id == 111


# ── /watches manager ─────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self) -> None:
        self.edited: dict[str, Any] | None = None
        self.sent: str | None = None

    async def edit_message(self, **kwargs: Any) -> None:
        self.edited = kwargs

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.sent = content

    async def defer(self) -> None:
        return None


def interaction(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=int(user_id)), response=FakeResponse())


async def test_watch_manager_mutes_and_removes_only_own_watches(sessions):
    await _history(sessions, {})
    await _add(
        sessions,
        Watch(discord_user_id="7", listing_id=-1, max_price_cents=15000, kinds="deal"),
        Watch(discord_user_id="8", listing_id=-1),
    )
    async with sessions() as session:
        rows = await user_watch_rows(session, "7")
    now = datetime.now(UTC)
    assert "ping ≤ $150.00" in watch_line(rows[0], now)
    assert "alerts: Deal" in watch_line(rows[0], now)
    assert watches_embed(rows, now).title == "🔔 Your watches (1)"

    bot = SimpleNamespace(ctx=SimpleNamespace(sessions=sessions))
    view = WatchManager(bot, "7", rows)  # type: ignore[arg-type]
    stranger = interaction("8")
    assert not await view.interaction_check(stranger)  # type: ignore[arg-type]

    me = interaction("7")
    await view.apply(me, "mute")  # type: ignore[arg-type]
    assert me.response.sent == "Pick one or more watches first."

    view.selected = [rows[0].watch.id]
    me = interaction("7")
    await view.apply(me, "mute")  # type: ignore[arg-type]
    assert "Muted for 24h 1" in me.response.edited["content"]
    async with sessions() as session:
        assert (await session.get(Watch, rows[0].watch.id)).muted_until is not None

    me = interaction("7")
    await view.apply(me, "remove")  # type: ignore[arg-type]
    assert "You aren't watching anything now" in me.response.edited["content"]
    async with sessions() as session:
        owners = list(await session.scalars(select(Watch.discord_user_id)))
    assert owners == ["8"]  # the other member's watch is untouched
