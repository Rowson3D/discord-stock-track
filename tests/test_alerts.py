from __future__ import annotations

from datetime import timedelta
from typing import Any

import discord
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.alerts import AlertRouter, is_price_drop
from tracker.bot.embeds import alert_content, alert_embed, digest_embed
from tracker.bot.sink import DiscordSink
from tracker.db.models import Listing, Watch
from tracker.domain import EventKind, StockStatus
from tracker.guilds import GuildConfig
from tracker.ingest import Ingestor
from tracker.outbox import RadarEvent, load_events
from tracker.settings import DiscordSettings, SmsSettings
from tracker.sms import SmsSink, sms_body

ALERTS, RADAR = 111, 222


def discord_settings(**overrides: object) -> DiscordSettings:
    values: dict[str, object] = {"channel_id": ALERTS, "radar_channel_id": RADAR}
    values.update(overrides)
    return DiscordSettings(_env_file=None, **values)  # type: ignore[call-arg]


async def _history(sessions: async_sessionmaker[AsyncSession], *steps: dict[str, Any]) -> None:
    """Ingest a sequence of U7-Pro states one minute apart (first one is the baseline)."""
    async with sessions() as session, session.begin():
        ingestor = Ingestor(session)
        for minute, overrides in enumerate(steps):
            await ingestor.ingest(
                [make_snapshot(**overrides)], observed_at=T0 + timedelta(minutes=minute)
            )


async def _pending(sessions: async_sessionmaker[AsyncSession]) -> list[RadarEvent]:
    async with sessions() as session:
        return await load_events(session, pending_only=True, limit=100)


async def _watch(sessions, user: str, *, global_: bool = False, max_price: int | None = None):
    async with sessions() as session, session.begin():
        listing_id = None if global_ else await session.scalar(select(Listing.id))
        session.add(Watch(discord_user_id=user, listing_id=listing_id, max_price_cents=max_price))


async def _plan(sessions, settings: DiscordSettings | None = None):
    async with sessions() as session:
        return await AlertRouter().plan(
            session,
            await _pending(sessions),
            GuildConfig.from_settings(settings or discord_settings()),
        )


async def test_back_in_stock_pings_watchers_subscribers_and_role(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    await _watch(sessions, "1")
    await _watch(sessions, "2", global_=True)
    plans = await _plan(sessions, discord_settings(alert_mention="<@&999>"))

    sold_out, back = plans
    assert sold_out.event.kind is EventKind.SOLD_OUT
    assert not sold_out.important
    assert sold_out.channel_id == RADAR
    assert not sold_out.pings
    assert back.event.kind is EventKind.BACK_IN_STOCK
    assert back.important
    assert back.channel_id == ALERTS
    assert back.mention_user_ids == ["1", "2"]
    assert back.mentions == "<@1> <@2> <@&999>"


async def test_max_price_filters_watcher_pings(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    await _watch(sessions, "cheap", max_price=15000)
    await _watch(sessions, "fine", max_price=20000)
    back = (await _plan(sessions))[-1]
    assert back.mention_user_ids == ["fine"]


async def test_price_drop_pings_watchers_only(sessions):
    await _history(sessions, {}, {"price_cents": 16900})
    await _watch(sessions, "1")
    await _watch(sessions, "2", global_=True)
    [drop] = await _plan(sessions)
    assert is_price_drop(drop.event)
    assert drop.important
    assert drop.channel_id == ALERTS
    assert drop.mention_user_ids == ["1"]


async def test_price_increase_is_routine(sessions):
    await _history(sessions, {}, {"price_cents": 19900})
    await _watch(sessions, "1")
    [rise] = await _plan(sessions)
    assert not is_price_drop(rise.event)
    assert not rise.important
    assert not rise.pings
    assert rise.channel_id == RADAR


async def test_flapping_is_suppressed_by_cooldown(sessions):
    out = {"status": StockStatus.OUT_OF_STOCK}
    await _history(sessions, {}, out, {}, out, {})  # back in stock twice within 4 minutes
    plans = await _plan(sessions)
    backs = [p for p in plans if p.event.kind is EventKind.BACK_IN_STOCK]
    assert [p.suppressed for p in backs] == [None, "cooldown"]
    assert not (await _plan(sessions, discord_settings(alert_cooldown_minutes=0)))[-1].suppressed


async def test_radar_channel_defaults_to_alert_channel(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK})
    [plan] = await _plan(sessions, discord_settings(radar_channel_id=None))
    assert plan.channel_id == ALERTS


# ── Discord sink ──────────────────────────────────────────────────────────


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class FakeClient:
    def __init__(self) -> None:
        self.channels = {ALERTS: FakeChannel(ALERTS), RADAR: FakeChannel(RADAR)}

    async def wait_until_ready(self) -> None:
        return None

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        raise discord.NotFound(httpx_response(404), {"code": 10003, "message": "Unknown Channel"})


def httpx_response(status: int) -> Any:
    class _Response:
        def __init__(self) -> None:
            self.status = status
            self.reason = "Not Found"

    return _Response()


async def test_sink_posts_alerts_with_buttons_and_mentions(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    await _watch(sessions, "1")
    client = FakeClient()
    sink = DiscordSink(client, sessions, AlertRouter(), discord_settings())
    await sink.publish(await _pending(sessions))

    [routine] = client.channels[RADAR].sent
    [alert] = client.channels[ALERTS].sent
    assert routine["embed"].title.startswith("🔴 Sold out")
    assert "<@1>" not in routine["content"]
    assert alert["content"].startswith("🟢 **Back in stock** · U7-Pro · $189.00 <@1>")
    assert alert["embed"].url == "https://store.ui.com/us/en/products/u7-pro"
    assert alert["allowed_mentions"].users is True
    labels = [getattr(item, "item", item).label for item in alert["view"].children]
    assert labels == ["Open", "Watch"]


async def test_sink_digests_many_routine_events(sessions):
    async with sessions() as session, session.begin():
        ingestor = Ingestor(session)
        snaps = [make_snapshot(external_id=f"X{i}-US", sku=f"X{i}") for i in range(8)]
        await ingestor.ingest(snaps, observed_at=T0)
        await ingestor.ingest(
            [
                make_snapshot(external_id=f"X{i}-US", sku=f"X{i}", price_cents=19900)
                for i in range(8)
            ],
            observed_at=T0 + timedelta(minutes=1),
        )
    client = FakeClient()
    settings = discord_settings(digest_threshold=5)
    await DiscordSink(client, sessions, AlertRouter(), settings).publish(await _pending(sessions))
    [digest] = client.channels[RADAR].sent
    assert digest["embed"].title == "📡 Radar digest · 8 updates"
    assert digest["embed"].description.count("\n") == 7


async def test_sink_skips_missing_channel(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK})
    client = FakeClient()
    settings = discord_settings(radar_channel_id=333)
    await DiscordSink(client, sessions, AlertRouter(), settings).publish(await _pending(sessions))
    assert all(not channel.sent for channel in client.channels.values())


def test_embed_rendering_for_restock_eta_and_digest():
    event = RadarEvent(
        id=1,
        kind=EventKind.RESTOCK_ETA,
        occurred_at=T0,
        old_value=None,
        new_value="2026-12-01",
        listing_id=1,
        source="unifi",
        region="ca",
        url="https://ca.store.ui.com/ca/en/products/ux7",
        sku="UX7-3",
        product_name="Dream Router 7",
        category="wifi-whole-home-wifi",
        pack_size=3,
        status=StockStatus.OUT_OF_STOCK,
        price_cents=55900,
        currency="CAD",
        restock_eta=None,
        is_early_access=False,
    )
    embed = alert_embed(event)
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "📅 Restock ETA: Dream Router 7"
    assert fields["SKU"] == "UX7-3 (3-pack)"
    assert fields["Price"] == "CA$559.00"
    assert fields["Change"] == "— → 2026-12-01"
    assert embed.footer.text == "Ubiquiti Store · CA · wifi-whole-home-wifi"
    from tracker.alerts import AlertPlan

    assert alert_content(AlertPlan(event, False, None)) == (
        "📅 **Restock ETA** · UX7-3 · CA$559.00 · CA"
    )
    assert (
        "[UX7-3](https://ca.store.ui.com/ca/en/products/ux7) CA"
        in digest_embed([event]).description
    )


# ── SMS ───────────────────────────────────────────────────────────────────


async def test_sms_sink_sends_only_important_kinds(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    settings = SmsSettings(  # type: ignore[call-arg]
        _env_file=None,
        enabled=True,
        TWILIO_ACCOUNT_SID="AC123",
        TWILIO_AUTH_TOKEN="secret",
        TWILIO_FROM_NUMBER="+15550000000",
        to_numbers="+15551111111,+15552222222",
    )
    assert settings.configured
    sink = SmsSink(settings, sessions, AlertRouter(), transport=httpx.MockTransport(handler))
    await sink.publish(await _pending(sessions))

    assert len(requests) == 2  # back_in_stock to two numbers; sold_out is not SMS-worthy
    assert requests[0].url.path == "/2010-04-01/Accounts/AC123/Messages.json"
    body = requests[0].content.decode()
    assert "To=%2B15551111111" in body
    assert "BACK+IN+STOCK" in body


def test_sms_body_is_compact():
    event = RadarEvent(
        1, EventKind.EARLY_ACCESS, T0, None, "U7-Pro", 1, "unifi", "us",
        "https://store.ui.com/us/en/products/u7-pro", "U7-Pro", "AP", None, 1,
        StockStatus.IN_STOCK, 18900, "USD", None, True,
    )  # fmt: skip
    assert sms_body(event) == (
        "EARLY ACCESS: U7-Pro | $189.00 | US | https://store.ui.com/us/en/products/u7-pro"
    )


@pytest.mark.parametrize("kinds", ["back_in_stock", "back_in_stock, early_access"])
def test_mention_kinds_parse(kinds: str):
    settings = DiscordSettings(_env_file=None, mention_kinds=kinds)  # type: ignore[call-arg]
    assert settings.mention_kinds[0] == "back_in_stock"
