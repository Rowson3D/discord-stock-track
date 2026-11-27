"""The watchdog tells the owner when tracking breaks, and when it's fixed."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from tests.conftest import T0, make_snapshot
from tracker.db.models import Listing
from tracker.ingest import Ingestor
from tracker.watchdog import Watchdog


async def test_a_stalled_crawl_and_stuck_alerts_reach_the_owner(sessions: Any) -> None:
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest([make_snapshot()], observed_at=T0)
    sent: list[tuple[str, str]] = []

    async def notify(title: str, text: str) -> None:
        sent.append((title, text))

    now = [T0 + timedelta(minutes=5)]
    dispatcher = SimpleNamespace(failing={}, expired=0)
    job = SimpleNamespace(name="unifi:hot", failures=0)
    scheduler = SimpleNamespace(jobs={"unifi:hot": job})
    dog = Watchdog(
        sessions,
        scheduler,  # type: ignore[arg-type]
        dispatcher,  # type: ignore[arg-type]
        notify,
        regions=["us"],
        crawl_interval_seconds=300,
        fastlane=SimpleNamespace(failures=0),
        clock=lambda: now[0],
    )

    await dog.check()
    assert sent == []  # all fine

    now[0] = T0 + timedelta(minutes=30)
    job.failures = 6
    dispatcher.failing = {"discord": "TimeoutError: "}
    await dog.check()
    title, text = sent[-1]
    assert title == "⚠️ FlipScout needs attention"
    assert "hasn't been checked for 30 min" in text
    assert "`unifi:hot` has failed 6 times" in text
    assert "discord" not in text  # a destination gets 5 minutes to recover first

    await dog.check()
    assert len(sent) == 1  # told once, not every minute

    now[0] += timedelta(minutes=6)
    await dog.check()
    assert "Alerts can't reach discord" in sent[-1][1]

    async with sessions() as session, session.begin():
        listing = await session.scalar(select(Listing))
        listing.last_checked_at = now[0]
    job.failures = 0
    dispatcher.failing = {}
    dispatcher.expired = 2
    await dog.check()
    titles = [t for t, _ in sent[-2:]]
    assert "✅ FlipScout is back to normal" in titles
    assert "⚠️ FlipScout dropped alerts" in titles
