"""Instant phone push through ntfy for the owner's own watches and rules.

Only events that the owner's watches (with their alert types, target price and mute) or
rules would ping for are pushed; "ping me for every important alert" doesn't count here, so
the phone only rings for what was chosen. Restocks and Early Access are urgent (priority 5),
the rest high (4). Tapping a notification opens the store page.

Works with the free ntfy.sh service (instant on iPhone, since ntfy.sh relays through Apple)
or a self-hosted server. On ntfy.sh the topic name is the password: `tracker ntfy setup`
makes a long random one.
"""

from __future__ import annotations

import argparse
import logging
import secrets
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.alerts import AlertRouter
from tracker.db.models import Watch, WatchRule
from tracker.domain import EventKind, format_cents, region_tag
from tracker.outbox import RadarEvent
from tracker.settings import ENV_FILE, NtfySettings, Settings
from tracker.sources.web import SOURCE_NAMES, SourceKey
from tracker.watching import ALERT_KEYS, alert_key, rule_matches, watch_pings

logger = logging.getLogger(__name__)

URGENT = frozenset({EventKind.BACK_IN_STOCK, EventKind.RELISTED, EventKind.EARLY_ACCESS})
TAGS = {
    "back_in_stock": "green_circle",
    "early_access": "zap",
    "price_drop": "chart_with_downwards_trend",
    "deal": "moneybag",
    "new_listing": "new",
    "restock_eta": "calendar",
    "sold_out": "red_circle",
}


def _store(event: RadarEvent) -> str:
    try:
        name = SOURCE_NAMES[SourceKey(event.source or "")]
    except ValueError:
        name = event.source or "Store"
    tag = region_tag(event.region)
    return f"{name} {tag}" if tag else name


def ntfy_message(event: RadarEvent, topic: str) -> dict[str, Any]:
    """The JSON body for one alert (ntfy's publish-as-JSON format)."""
    key = alert_key(event)
    label = ALERT_KEYS.get(key or "", event.kind.value.replace("_", " ").capitalize())
    if event.kind is EventKind.RELISTED:
        label = "Relisted"
    name = event.product_name or event.sku or "Item"
    lines = [
        f"{event.sku or name} · {format_cents(event.price_cents, event.currency)} · {_store(event)}"
    ]
    if key == "price_drop" or event.kind is EventKind.RESTOCK_ETA:
        lines.append(f"{event.old_value or '—'} → {event.new_value or '—'}")
    if event.restock_eta and event.kind is not EventKind.RESTOCK_ETA:
        lines.append(f"Restock ETA {event.restock_eta.isoformat()}")
    if event.profit_text:
        lines.append(f"Flip {event.profit_text}")
    message: dict[str, Any] = {
        "topic": topic,
        "title": f"{label}: {name}"[:200],
        "message": "\n".join(lines),
        "priority": 5 if event.kind in URGENT else 4,
        "tags": [TAGS.get(key or "", "bell")],
    }
    if event.url:
        message["click"] = event.url
        message["actions"] = [{"action": "view", "label": "Open store", "url": event.url}]
    return message


class NtfyClient:
    def __init__(
        self, settings: NtfySettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    @property
    def topic(self) -> str:
        assert self._settings.topic is not None
        return self._settings.topic.get_secret_value()

    async def send(self, messages: Sequence[dict[str, Any]]) -> int:
        """Publish messages; returns how many ntfy accepted (failures are logged)."""
        headers = {}
        if self._settings.token:
            headers["Authorization"] = f"Bearer {self._settings.token.get_secret_value()}"
        sent = 0
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport, headers=headers
        ) as client:
            for message in messages:
                try:
                    response = await client.post(self._settings.server, json=message)
                except httpx.HTTPError as exc:
                    logger.error("ntfy push failed: %s", exc)
                    continue
                if response.status_code >= 400:
                    logger.error(
                        "ntfy push failed: HTTP %s %s",
                        response.status_code,
                        response.text[:200],
                    )
                    continue
                sent += 1
        return sent


class NtfySink:
    name = "ntfy"

    def __init__(
        self,
        settings: NtfySettings,
        sessions: async_sessionmaker[AsyncSession],
        owner_ids: Callable[[], Iterable[str]],
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        cooldown_minutes: float = 10.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._owner_ids = owner_ids
        self._client = NtfyClient(settings, transport)
        self._cooldown = cooldown_minutes
        self._clock = clock
        self._server_kinds = frozenset(
            EventKind(k) for k in settings.kinds if k in EventKind._value2member_map_
        )

    async def chosen(self, session: AsyncSession, events: Sequence[RadarEvent]) -> list[RadarEvent]:
        """Events the owner's own watches or rules ping for (and not in cooldown)."""
        owners = sorted(set(self._owner_ids()))
        listing_ids = {e.listing_id for e in events if e.listing_id is not None}
        if not owners or not events:
            return []
        watches: dict[int, list[Watch]] = {}
        for watch in await session.scalars(
            select(Watch).where(
                Watch.discord_user_id.in_(owners), Watch.listing_id.in_(listing_ids)
            )
        ):
            watches.setdefault(watch.listing_id or -1, []).append(watch)
        rules = list(
            await session.scalars(
                select(WatchRule).where(WatchRule.enabled, WatchRule.discord_user_id.in_(owners))
            )
        )
        now = self._clock()
        picked = []
        for event in events:
            wanted = any(
                watch_pings(w, event, self._server_kinds, now)
                for w in watches.get(event.listing_id or -1, [])
            ) or any(rule_matches(r, event, now) for r in rules)
            if wanted and not await AlertRouter._recently_alerted(session, event, self._cooldown):
                picked.append(event)
        return picked

    async def publish(self, events: Sequence[RadarEvent]) -> None:
        async with self._sessions() as session:
            picked = await self.chosen(session, events)
        if picked:
            topic = self._client.topic
            await self._client.send([ntfy_message(e, topic) for e in picked])


# ── command line: tracker ntfy setup | show | test ──────────────────────────


def new_topic() -> str:
    return f"flipscout-{secrets.token_urlsafe(24).replace('_', '').replace('-', '')[:28]}"


def subscribe_url(settings: NtfySettings) -> str:
    assert settings.topic is not None
    return f"{settings.server}/{settings.topic.get_secret_value()}"


def _print_subscription(settings: NtfySettings) -> None:
    from tracker.panel.setup import _show_qr

    url = subscribe_url(settings)
    print("\nOn your phone: install the free ntfy app, tap +, and subscribe to this topic")
    print(f"(server {settings.server}). Treat it like a password.\n")
    print(f"  Topic: {settings.topic.get_secret_value() if settings.topic else ''}")
    print(f"  Link:  {url}\n")
    _show_qr(url)


async def cmd_ntfy_setup(settings: Settings, args: argparse.Namespace) -> int:
    from tracker.panel.setup import set_env_value

    env_file: Path = args.env_file or ENV_FILE
    if settings.ntfy.configured and not args.rotate:
        print("ntfy is already set up. Use `tracker ntfy show`, or --rotate for a new topic.")
        return 0
    topic = new_topic()
    set_env_value(env_file, "NTFY_TOPIC", topic)
    settings.ntfy = NtfySettings(_env_file=env_file)  # type: ignore[call-arg]
    print(f"Saved NTFY_TOPIC to {env_file}. Restart FlipScout to start pushing alerts.")
    _print_subscription(settings.ntfy)
    return 0


async def cmd_ntfy_show(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.ntfy.configured:
        print("ntfy isn't set up: run `tracker ntfy setup` (or set NTFY_TOPIC).")
        return 1
    _print_subscription(settings.ntfy)
    return 0


async def cmd_ntfy_test(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.ntfy.configured:
        print("ntfy isn't set up: run `tracker ntfy setup` (or set NTFY_TOPIC).")
        return 1
    client = NtfyClient(settings.ntfy)
    sent = await client.send(
        [
            {
                "topic": client.topic,
                "title": "FlipScout test alarm",
                "message": "If this rang, restock alerts will reach you instantly.",
                "priority": 5,
                "tags": ["white_check_mark"],
            }
        ]
    )
    print("Sent a test push." if sent else "The test push failed (see the log above).")
    return 0 if sent else 1


def add_ntfy_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ntfy = commands.add_parser("ntfy", help="instant phone push (ntfy)").add_subparsers(
        dest="ntfy_command", required=True
    )
    setup = ntfy.add_parser("setup", help="create a private topic and show how to subscribe")
    setup.add_argument("--rotate", action="store_true", help="replace the existing topic")
    setup.add_argument("--env-file", type=Path, help=argparse.SUPPRESS)
    setup.set_defaults(handler=cmd_ntfy_setup)
    ntfy.add_parser("show", help="show the topic and QR code to subscribe").set_defaults(
        handler=cmd_ntfy_show
    )
    ntfy.add_parser("test", help="send a test alarm").set_defaults(handler=cmd_ntfy_test)
