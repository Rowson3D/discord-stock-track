"""ntfy phone push: which events ring the owner's phone, the message format, and the CLI."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from tests.conftest import T0
from tests.test_alerts import _history, _pending
from tracker.db.models import Listing, Watch, WatchRule
from tracker.domain import EventKind, StockStatus
from tracker.ntfy import (
    NtfyClient,
    NtfySink,
    cmd_ntfy_setup,
    cmd_ntfy_test,
    new_topic,
    ntfy_message,
)
from tracker.settings import NtfySettings, Settings

OWNER = "123456789012345670"
TOPIC = "flipscout-testtopic"


def settings(**values) -> NtfySettings:
    base = {"topic": TOPIC, "server": "https://ntfy.example/"}
    base.update(values)
    return NtfySettings(_env_file=None, **base)  # type: ignore[call-arg]


class Recorder:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json={"id": "x"})

        return httpx.MockTransport(handle)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


async def restock_events(sessions):
    """A sold-out then back-in-stock U7-Pro (two pending events)."""
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    return await _pending(sessions)


async def add(sessions, *rows):
    async with sessions() as session, session.begin():
        listing_id = await session.scalar(select(Listing.id))
        for row in rows:
            if isinstance(row, Watch) and row.listing_id == -1:
                row.listing_id = listing_id
            session.add(row)


def sink(sessions, recorder: Recorder, **values) -> NtfySink:
    return NtfySink(
        settings(**values),
        sessions,
        lambda: [OWNER],
        recorder.transport(),
        clock=lambda: T0 + timedelta(minutes=5),
    )


async def test_owner_watch_rings_with_urgent_priority(sessions):
    events = await restock_events(sessions)
    await add(sessions, Watch(discord_user_id=OWNER, listing_id=-1))
    recorder = Recorder()
    await sink(sessions, recorder).publish(events)
    [body] = recorder.bodies
    assert recorder.requests[0].url == "https://ntfy.example"
    assert body["topic"] == TOPIC
    assert body["title"] == "Back in stock: Access Point U7 Pro"
    assert body["priority"] == 5
    assert body["tags"] == ["green_circle"]
    assert body["click"] == "https://store.ui.com/us/en/products/u7-pro"
    assert body["actions"][0]["url"] == body["click"]
    assert "U7-Pro · $189.00 · Ubiquiti Store" in body["message"]


async def test_only_what_the_owner_chose(sessions):
    events = await restock_events(sessions)
    await add(
        sessions,
        Watch(discord_user_id="555", listing_id=-1),  # a member's watch
        Watch(discord_user_id=OWNER, listing_id=None),  # "ping me for everything"
    )
    recorder = Recorder()
    await sink(sessions, recorder).publish(events)
    assert recorder.requests == []

    await add(
        sessions,
        Watch(discord_user_id=OWNER, listing_id=-1, muted_until=T0 + timedelta(days=1)),
    )
    await sink(sessions, recorder).publish(events)
    assert recorder.requests == []  # muted

    await add(sessions, WatchRule(discord_user_id=OWNER, name="u7", query="u7", kinds="sold_out"))
    await sink(sessions, recorder).publish(events)
    [body] = recorder.bodies
    assert body["title"].startswith("Sold out")
    assert body["priority"] == 4


async def test_alert_types_and_server_defaults(sessions):
    events = await restock_events(sessions)
    await add(sessions, Watch(discord_user_id=OWNER, listing_id=-1, kinds="sold_out"))
    recorder = Recorder()
    await sink(sessions, recorder).publish(events)
    assert [b["title"].split(":")[0] for b in recorder.bodies] == ["Sold out"]

    recorder = Recorder()
    async with sessions() as session, session.begin():
        (await session.scalar(select(Watch))).kinds = None  # server defaults
    await sink(sessions, recorder, kinds="early_access").publish(events)
    assert recorder.requests == []  # back in stock isn't in this server default list


async def test_failures_are_logged_not_raised(sessions, caplog):
    events = await restock_events(sessions)
    await add(sessions, Watch(discord_user_id=OWNER, listing_id=-1))
    recorder = Recorder(status=429)
    await sink(sessions, recorder).publish(events)
    assert "ntfy push failed: HTTP 429" in caplog.text

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = NtfyClient(settings(), httpx.MockTransport(boom))
    assert await client.send([{"topic": TOPIC, "message": "x"}]) == 0


async def test_access_token_header(sessions):
    recorder = Recorder()
    client = NtfyClient(settings(token="tk_secret"), recorder.transport())
    assert await client.send([{"topic": TOPIC, "message": "x"}]) == 1
    assert recorder.requests[0].headers["authorization"] == "Bearer tk_secret"


async def test_price_drop_message(sessions):
    await _history(sessions, {}, {"price_cents": 15900})
    [event] = await _pending(sessions)
    assert event.kind is EventKind.PRICE_CHANGE
    body = ntfy_message(event, TOPIC)
    assert body["title"] == "Price drop: Access Point U7 Pro"
    assert body["priority"] == 4
    assert "$189.00 → $159.00" in body["message"]


def test_settings_validation():
    with pytest.raises(ValidationError):
        settings(topic="has spaces!")
    with pytest.raises(ValidationError):
        settings(server="ntfy.sh")
    assert settings().server == "https://ntfy.example"
    assert not settings(topic=None).configured
    assert not settings(enabled=False).configured
    topic = new_topic()
    assert topic.startswith("flipscout-")
    assert len(topic) >= 30
    assert NtfySettings(_env_file=None, topic=topic).configured  # type: ignore[call-arg]


async def test_setup_writes_a_private_topic(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("DISCORD_BOT_TOKEN=x\n", encoding="utf-8")
    config = Settings(env_file=None)
    args = argparse.Namespace(env_file=env, rotate=False)
    assert await cmd_ntfy_setup(config, args) == 0
    text = env.read_text(encoding="utf-8")
    assert text.startswith("DISCORD_BOT_TOKEN=x\n")
    topic = text.split("NTFY_TOPIC=")[1].strip()
    assert topic.startswith("flipscout-")
    assert topic in capsys.readouterr().out  # shown once, in your own terminal

    assert await cmd_ntfy_setup(config, args) == 0  # already set up: unchanged
    assert env.read_text(encoding="utf-8").count("NTFY_TOPIC=") == 1


async def test_test_command_needs_a_topic():
    assert await cmd_ntfy_test(Settings(env_file=None), argparse.Namespace()) == 1
