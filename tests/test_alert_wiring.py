"""Alert destinations registered at startup actually receive events.

Regression: the dispatcher copied its sink list at construction, and Discord, SMS and ntfy
were appended to the original list afterwards, so every alert was logged, marked as
delivered, and never posted.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from tests.conftest import T0
from tests.test_alerts import _history
from tracker.alerts import AlertRouter
from tracker.app import register_alert_sinks
from tracker.domain import StockStatus
from tracker.outbox import EventDispatcher, LoggingSink
from tracker.settings import DiscordSettings, NtfySettings, Settings

ALERTS = 111


class FakeChannel:
    id = ALERTS
    guild = None

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(jump_url="https://discord.com/channels/1/2/3")


class FakeBot:
    def __init__(self) -> None:
        self.channel = FakeChannel()
        self.owner_ids = {"42"}

    async def wait_until_ready(self) -> None:
        return None

    def get_channel(self, channel_id: int) -> FakeChannel:
        return self.channel

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        return self.channel


class Recording:
    name = "recording"

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, events) -> None:
        self.events.extend(events)


def settings(**ntfy: Any) -> Settings:
    config = Settings(env_file=None)
    config.discord = DiscordSettings(_env_file=None, channel_id=ALERTS)  # type: ignore[call-arg]
    config.ntfy = NtfySettings(_env_file=None, **ntfy)  # type: ignore[call-arg]
    return config


def dispatcher(sessions) -> EventDispatcher:
    return EventDispatcher(sessions, [LoggingSink()], clock=lambda: T0 + timedelta(minutes=5))


async def test_sinks_added_after_construction_receive_events(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK})
    outbox = dispatcher(sessions)
    late = Recording()
    outbox.add_sink(late)
    assert outbox.sink_names == ["log", "recording"]
    assert await outbox.dispatch() == 1
    assert [e.kind.value for e in late.events] == ["sold_out"]


async def test_registered_discord_sink_posts_real_alerts(sessions):
    await _history(sessions, {}, {"status": StockStatus.OUT_OF_STOCK}, {})
    outbox = dispatcher(sessions)
    bot = FakeBot()
    names = register_alert_sinks(settings(), sessions, outbox, AlertRouter(), bot)  # type: ignore[arg-type]
    assert names == ["log", "discord"]
    assert await outbox.dispatch() == 2
    contents = [message["content"] for message in bot.channel.sent]
    assert any("Back in stock" in text for text in contents)


async def test_ntfy_and_headless_registration(sessions):
    outbox = dispatcher(sessions)
    names = register_alert_sinks(
        settings(topic="flipscout-wiring"),
        sessions,
        outbox,
        AlertRouter(),
        FakeBot(),  # type: ignore[arg-type]
    )
    assert names == ["log", "discord", "ntfy"]
    headless = dispatcher(sessions)
    assert register_alert_sinks(settings(), sessions, headless, AlertRouter(), None) == ["log"]
