"""The drop detector: conditional checks of the snatch list, how often, and the alert path."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from tracker.db.models import Event, Listing
from tracker.domain import EventKind
from tracker.ingest import Ingestor
from tracker.services.fastlane import FastLane, busy_hours, pacific
from tracker.settings import UnifiSettings
from tracker.sources.unifi.parser import extract_next_data, parse_product_page

# Tuesday 2026-11-24, 10:00 Pacific.
TUESDAY_MORNING = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)


def test_busy_hours_are_weekday_daytime_pacific() -> None:
    tz = pacific()
    assert busy_hours(TUESDAY_MORNING, tz)
    assert not busy_hours(TUESDAY_MORNING + timedelta(hours=10), tz)  # 8pm
    assert not busy_hours(TUESDAY_MORNING + timedelta(days=4), tz)  # Saturday


class Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self) -> int:
        self.calls += 1
        return 0


def sold_out(page_props: dict[str, Any]) -> dict[str, Any]:
    props = copy.deepcopy(page_props)
    for product in props["collection"]["products"]:
        product["status"] = "SoldOut"
        for variant in product.get("variants", []):
            variant["status"] = "SoldOut"
    return props


class Store:
    """store.ui.com as the lane sees it: the page (for the build id) and its page data."""

    def __init__(self, page_props: dict[str, Any]) -> None:
        self.page_props = page_props
        self.etag = '"v1"'
        self.build = "b1"
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/us/en/products/u7-pro":
            return httpx.Response(200, text=f'<script>{{"buildId":"{self.build}"}}</script>')
        if path == f"/_next/data/{self.build}/us/en/products/u7-pro.json":
            if request.headers.get("if-none-match") == self.etag:
                return httpx.Response(304)
            body = json.dumps({"pageProps": self.page_props, "__N_SSP": True})
            return httpx.Response(200, text=body, headers={"etag": self.etag})
        return httpx.Response(404)


async def seed(sessions: Any, data: dict[str, Any]) -> None:
    snapshots = parse_product_page(data, base_url="https://store.ui.com")
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(snapshots, observed_at=TUESDAY_MORNING)
        for listing in await session.scalars(select(Listing)):
            listing.snatch = True


async def test_checks_cost_nothing_until_the_store_changes(
    sessions: Any, fixture_text: Callable[[str], str]
) -> None:
    data = extract_next_data(fixture_text("unifi/product_u7-pro.html"))
    await seed(sessions, data)
    store = Store(sold_out(data["props"]["pageProps"]))
    clock = [0.0]
    dispatcher = Dispatcher()
    lane = FastLane(
        sessions,
        UnifiSettings(_env_file=None),  # type: ignore[call-arg]
        dispatcher,  # type: ignore[arg-type]
        transport=httpx.MockTransport(store.handler),
        clock=lambda: TUESDAY_MORNING,
        monotonic=lambda: clock[0],
    )

    assert await lane.tick() > 0  # it sold out: changed data goes through the normal ingest
    assert dispatcher.calls == 1
    async with sessions() as session:
        kinds = set(await session.scalars(select(Event.kind)))
    assert EventKind.SOLD_OUT.value in kinds
    assert store.requests[0].headers["user-agent"].startswith("FlipScout/")

    sent = len(store.requests)
    clock[0] += 3
    assert await lane.tick() == 0
    assert len(store.requests) == sent  # not due yet: every 5s in weekday restock hours

    clock[0] += 3
    assert await lane.tick() == 0
    assert store.requests[-1].headers["if-none-match"] == '"v1"'  # answered 304, no body

    store.build = "b2"  # the store shipped a new site version
    clock[0] += 6
    await lane.tick()  # 404: forget the old build
    clock[0] += 6
    await lane.tick()
    assert store.requests[-1].url.path.startswith("/_next/data/b2/")
    assert (await lane.status()).fast


async def test_quiet_hours_check_every_twenty_seconds(
    sessions: Any, fixture_text: Callable[[str], str]
) -> None:
    data = extract_next_data(fixture_text("unifi/product_u7-pro.html"))
    await seed(sessions, data)
    store = Store(data["props"]["pageProps"])
    clock = [0.0]
    saturday_night = TUESDAY_MORNING + timedelta(days=4, hours=12)
    lane = FastLane(
        sessions,
        UnifiSettings(_env_file=None),  # type: ignore[call-arg]
        Dispatcher(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(store.handler),
        clock=lambda: saturday_night,
        monotonic=lambda: clock[0] + 10_000,  # no recent restock wave
    )
    await lane.tick()
    sent = len(store.requests)
    clock[0] += 6
    await lane.tick()
    assert len(store.requests) == sent
    clock[0] += 15
    await lane.tick()
    assert len(store.requests) == sent + 1
    status = await lane.status()
    assert not status.fast
    assert status.items == 1


async def test_nothing_on_the_list_means_no_requests(sessions: Any) -> None:
    store = Store({})
    lane = FastLane(
        sessions,
        UnifiSettings(_env_file=None),  # type: ignore[call-arg]
        Dispatcher(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(store.handler),
    )
    assert await lane.tick() == 0
    assert store.requests == []
