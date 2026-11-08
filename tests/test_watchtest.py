"""Watch tests: what a watch alerts you about, why, and the sample alert sent to Discord."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from tests.conftest import T0
from tests.test_alerts import ALERTS, RADAR, _history, discord_settings
from tests.test_panel import WRITE, Clock, audit_actions, login, make_client, panel_settings
from tracker.bot.sink import sample_alert_message, send_test_alert
from tracker.bot.views import WatchManager, watch_test_embed
from tracker.db.models import Guild, Listing, Watch
from tracker.domain import EventKind
from tracker.settings import Settings, SmsSettings
from tracker.watching import user_watch_rows
from tracker.watchtest import SampleAlertError, WatchTest, plan_watch_test

OWNER = "123456789012345670"


def settings(**discord: Any) -> Settings:
    base = panel_settings()
    base.discord = discord_settings(owner_ids=OWNER, **discord)
    return base


async def add_watch(sessions, **values: Any) -> int:
    await _history(sessions, {})
    async with sessions() as session, session.begin():
        listing_id = await session.scalar(select(Listing.id))
        watch = Watch(discord_user_id=OWNER, listing_id=listing_id, **values)
        session.add(watch)
        await session.flush()
        return watch.id


async def plan(sessions, watch_id: int, config: Settings | None = None, **kwargs: Any) -> WatchTest:
    async with sessions() as session:
        test = await plan_watch_test(session, config or settings(), watch_id, now=T0, **kwargs)
    assert test is not None
    return test


def by_kind(test: WatchTest) -> dict[str, Any]:
    return {item.key: item for item in test.coverage}


async def test_server_defaults_explain_every_alert_type(sessions):
    watch_id = await add_watch(sessions)
    test = await plan(sessions, watch_id)
    assert test.selected.key == "back_in_stock"
    assert test.event.kind is EventKind.BACK_IN_STOCK
    assert test.event.listing_id is not None
    assert test.event.id == 0
    cover = by_kind(test)
    assert cover["back_in_stock"].pings
    assert cover["back_in_stock"].channel_id == ALERTS
    assert "by default" in cover["back_in_stock"].reason
    assert cover["price_drop"].pings  # drops on anything you watch always ping
    assert test.coverage[0].key == "back_in_stock"
    sold_out = cover["sold_out"]
    assert not sold_out.pings
    assert sold_out.posted
    assert sold_out.channel_id == RADAR
    assert "server's defaults" in sold_out.reason
    assert "without a ping" in sold_out.reason
    assert test.mention_user_ids == [OWNER]


async def test_custom_types_target_price_and_mute(sessions):
    watch_id = await add_watch(sessions, kinds="price_drop,sold_out")
    test = await plan(sessions, watch_id)
    assert test.selected.key == "price_drop"  # the first type you picked
    assert test.event.old_value == "$207.90"
    assert test.event.new_value == "$189.00"
    cover = by_kind(test)
    assert cover["sold_out"].pings
    assert cover["sold_out"].important  # you asked for it, so it goes to the alert channel
    assert cover["back_in_stock"].reason.startswith("You turned off Back in stock")

    async with sessions() as session, session.begin():
        watch = await session.get(Watch, watch_id)
        watch.max_price_cents = 15000
    cover = by_kind(await plan(sessions, watch_id))
    assert not cover["price_drop"].pings
    assert "at or below $150.00" in cover["price_drop"].reason

    async with sessions() as session, session.begin():
        watch = await session.get(Watch, watch_id)
        watch.max_price_cents = None
        watch.muted_until = T0 + timedelta(hours=2)
    test = await plan(sessions, watch_id)
    assert not any(item.pings for item in test.coverage)
    assert test.selected.reason.startswith("This watch is muted until")
    assert test.mention_user_ids == []

    async with sessions() as session, session.begin():
        watch = await session.get(Watch, watch_id)
        watch.kinds = ""
        watch.muted_until = None
    assert by_kind(await plan(sessions, watch_id))["deal"].reason == (
        "Every alert type is turned off for this watch. It still posts in the alert channel, "
        "without a ping."
    )


async def test_server_settings_unapproved_and_hidden_deals(sessions):
    watch_id = await add_watch(sessions, guild_id="42")
    async with sessions() as session, session.begin():
        session.add(Guild(guild_id="42", name="Home", status="approved", alert_channel_id="900"))
    test = await plan(sessions, watch_id)
    cover = by_kind(test)
    assert cover["back_in_stock"].channel_id == 900
    assert not cover["deal"].posted
    assert "hides valuations" in cover["deal"].reason
    assert not cover["sold_out"].pings
    assert cover["sold_out"].channel_id == 900

    async with sessions() as session, session.begin():
        (await session.get(Guild, "42")).status = "blocked"
    test = await plan(sessions, watch_id)
    assert not test.selected.posted
    assert "isn't approved" in test.selected.reason


async def test_no_server_and_sms(sessions):
    watch_id = await add_watch(sessions)
    config = settings(channel_id=None, radar_channel_id=None)
    test = await plan(sessions, watch_id, config)
    assert not test.selected.posted
    assert "No Discord server" in test.selected.reason

    config = settings()
    config.sms = SmsSettings(
        _env_file=None,  # type: ignore[call-arg]
        enabled=True,
        TWILIO_ACCOUNT_SID="AC1",
        TWILIO_AUTH_TOKEN="t",
        TWILIO_FROM_NUMBER="+15550000000",
        to_numbers="+15551111111",
    )
    cover = by_kind(await plan(sessions, watch_id, config))
    assert cover["back_in_stock"].sms
    assert not cover["sold_out"].sms


async def test_only_your_watch_and_known_types(sessions):
    watch_id = await add_watch(sessions)
    async with sessions() as session:
        assert await plan_watch_test(session, settings(), watch_id, user_id="999") is None
        assert await plan_watch_test(session, settings(), 12345) is None
        with pytest.raises(ValueError, match="unknown alert type"):
            await plan_watch_test(session, settings(), watch_id, key="nope")


# ── posting to Discord ───────────────────────────────────────────────────


class FakeChannel:
    def __init__(self, guild_id: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.guild = SimpleNamespace(id=guild_id) if guild_id else None

    async def send(self, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(jump_url="https://discord.com/channels/1/2/3")


class FakeClient:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.channel = channel

    async def wait_until_ready(self) -> None:
        return None

    def get_channel(self, channel_id: int) -> Any:
        return self.channel

    async def fetch_channel(self, channel_id: int) -> Any:
        return self.channel


async def test_sample_alert_marks_the_test_and_pings_only_you(sessions):
    watch_id = await add_watch(sessions)
    test = await plan(sessions, watch_id)
    message = sample_alert_message(test)
    assert message["content"].startswith("🧪 **Test alert** · 🟢 **Back in stock** · U7-Pro")
    assert message["content"].endswith(f"<@{OWNER}>")
    assert message["embed"].title.startswith("🧪 TEST · ")
    assert not message["allowed_mentions"].roles
    assert not message["allowed_mentions"].everyone
    labels = [item.label for item in message["view"].children]
    assert labels == ["Open"]  # no Watch button: pressing it would unwatch the item

    channel = FakeChannel()
    link = await send_test_alert(FakeClient(channel), test)
    assert link == "https://discord.com/channels/1/2/3"
    assert len(channel.sent) == 1

    muted = await plan(sessions, watch_id, key="sold_out")
    assert "@" not in sample_alert_message(muted)["content"]  # posts without a ping


async def test_sample_alert_refuses_bad_destinations(sessions):
    watch_id = await add_watch(sessions, guild_id="42")
    async with sessions() as session, session.begin():
        session.add(Guild(guild_id="42", status="approved", alert_channel_id="900"))
    test = await plan(sessions, watch_id)
    with pytest.raises(SampleAlertError, match="isn't in the watch's server"):
        await send_test_alert(FakeClient(FakeChannel(guild_id=7)), test)
    deal = await plan(sessions, watch_id, key="deal")
    with pytest.raises(SampleAlertError, match="hides valuations"):
        await send_test_alert(FakeClient(FakeChannel()), deal)


# ── panel ────────────────────────────────────────────────────────────────


class FakePoster:
    def __init__(self, error: str | None = None) -> None:
        self.tests: list[WatchTest] = []
        self.error = error

    async def post_test_alert(self, test: WatchTest) -> str:
        if self.error:
            raise SampleAlertError(self.error)
        self.tests.append(test)
        return "https://discord.com/channels/1/2/3"

    def channel_label(self, channel_id: int) -> str | None:
        return "#alerts" if channel_id == ALERTS else None


async def test_panel_explains_and_sends(sessions):
    watch_id = await add_watch(sessions, kinds="back_in_stock,sold_out")
    async with sessions() as session, session.begin():
        listing_id = await session.scalar(select(Listing.id))
        session.add(Watch(discord_user_id="555", listing_id=listing_id))
    member_id = watch_id + 1
    poster = FakePoster()
    clock = Clock()
    async with make_client(sessions, clock, settings(), alerts=poster) as c:
        await login(c, clock)
        url = f"/api/watches/{watch_id}/test"
        explained = (await c.post(url, json={}, headers=WRITE)).json()
        assert explained["sent"] is None
        assert explained["can_send"] is True
        assert explained["selected"]["kind"] == "back_in_stock"
        assert explained["selected"]["channel"] == "#alerts"
        cover = {item["kind"]: item for item in explained["coverage"]}
        assert cover["sold_out"]["pings"]
        assert cover["sold_out"]["channel"] == "#alerts"
        assert not cover["early_access"]["pings"]
        assert poster.tests == []

        sent = (await c.post(url, json={"kind": "sold_out", "send": True}, headers=WRITE)).json()
        assert sent["sent"] == {"ok": True, "link": "https://discord.com/channels/1/2/3"}
        assert poster.tests[0].selected.key == "sold_out"

        assert (await c.post(url, json={"kind": "x"}, headers=WRITE)).status_code == 422
        other = await c.post(f"/api/watches/{member_id}/test", json={}, headers=WRITE)
        assert other.status_code == 404  # members' watches would ping them, not you
        assert (await c.post(url, json={})).status_code == 403  # writes need the header
    assert "watch.test" in await audit_actions(sessions)

    clock = Clock()
    async with make_client(sessions, clock, settings(), alerts=FakePoster("no access")) as c:
        await login(c, clock)
        sent = (
            await c.post(f"/api/watches/{watch_id}/test", json={"send": True}, headers=WRITE)
        ).json()
        assert sent["sent"] == {"ok": False, "error": "no access"}
    clock = Clock()
    async with make_client(sessions, clock, settings()) as c:  # Discord not connected
        await login(c, clock)
        sent = (
            await c.post(f"/api/watches/{watch_id}/test", json={"send": True}, headers=WRITE)
        ).json()
        assert sent["can_send"] is False
        assert sent["selected"]["channel"] == f"channel {ALERTS}"
        assert sent["sent"]["ok"] is False


# ── /watches Test button ─────────────────────────────────────────────────


class Followup:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})


class DeferResponse:
    async def defer(self, **kwargs: Any) -> None:
        return None


async def test_watch_manager_test_button(sessions):
    watch_id = await add_watch(sessions)
    async with sessions() as session:
        rows = await user_watch_rows(session, OWNER)
    poster = FakePoster()
    bot = SimpleNamespace(
        ctx=SimpleNamespace(sessions=sessions, settings=settings()),
        post_test_alert=poster.post_test_alert,
    )
    view = WatchManager(bot, OWNER, rows)  # type: ignore[arg-type]
    assert "Test" in [getattr(item, "label", None) for item in view.children]
    view.selected = [watch_id]
    me = SimpleNamespace(
        user=SimpleNamespace(id=int(OWNER), __str__=lambda self: "owner"),
        guild_id=None,
        response=DeferResponse(),
        followup=Followup(),
    )
    await view.apply(me, "test")  # type: ignore[arg-type]
    [reply] = me.followup.sent
    assert reply["ephemeral"] is True
    embed = reply["embed"]
    assert "Sent a sample **Back in stock** alert" in embed.description
    assert f"✅ **Back in stock**: pings you in <#{ALERTS}>" in embed.description
    assert f"➖ **Sold out**: posts in <#{RADAR}>, no ping" in embed.description
    assert len(poster.tests) == 1
    assert "watch.test" in await audit_actions(sessions)

    failing = FakePoster("The bot can't post in the alert channel (Missing Access).")
    bot.post_test_alert = failing.post_test_alert
    me.followup = Followup()
    await view.apply(me, "test")  # type: ignore[arg-type]
    assert "Couldn't send" in me.followup.sent[0]["embed"].description


def test_watch_test_embed_without_a_message():
    item = SimpleNamespace(
        key="deal", label="Deal", pings=False, posted=False, channel_id=None, important=False,
        reason="This server hides valuations, so deals are never posted there.", sms=False,
    )  # fmt: skip
    test = SimpleNamespace(sku="U7-Pro", region="us", selected=item, coverage=[item])
    embed = watch_test_embed(test, None, None)  # type: ignore[arg-type]
    assert "🚫 **Deal**: not posted" in embed.description
