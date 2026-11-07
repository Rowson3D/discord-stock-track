from __future__ import annotations

import asyncio

import pytest

from tracker.scheduler import Job, Scheduler


async def _run_until(scheduler: Scheduler, condition, limit_seconds: float = 2.0) -> None:
    """Run the scheduler until `condition()` holds (test helper; polls by design)."""
    stop = asyncio.Event()
    runner = asyncio.create_task(scheduler.run(stop))
    try:
        async with asyncio.timeout(limit_seconds):
            while not condition():  # noqa: ASYNC110
                await asyncio.sleep(0.005)
    finally:
        stop.set()
        await runner


async def test_runs_job_repeatedly():
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    scheduler = Scheduler()
    scheduler.add("tick", 0.01, job, jitter=0)
    await _run_until(scheduler, lambda: calls >= 3)
    assert calls >= 3


async def test_job_is_single_flight_and_others_keep_running():
    active = 0
    max_active = 0
    fast_calls = 0
    release = asyncio.Event()

    async def slow():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await release.wait()
        active -= 1

    async def fast():
        nonlocal fast_calls
        fast_calls += 1

    scheduler = Scheduler()
    scheduler.add("slow", 0.001, slow, jitter=0)
    scheduler.add("fast", 0.005, fast, jitter=0)
    # The slow job blocks, yet the fast job keeps running and slow never overlaps itself.
    await _run_until(scheduler, lambda: fast_calls >= 5)
    release.set()
    assert max_active == 1


async def test_failures_back_off_and_recover():
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("boom")

    scheduler = Scheduler()
    job = scheduler.add("flaky", 0.005, flaky, jitter=0)
    await _run_until(scheduler, lambda: attempts >= 3)
    assert job.failures == 0  # recovered on the third attempt
    assert job.runs >= 3


def test_backoff_doubles_and_caps():
    job = Job(name="j", interval=10, func=lambda: None, max_backoff=60)  # type: ignore[arg-type]
    job.failures = 1
    assert job.delay_after(succeeded=False) == 20
    job.failures = 3
    assert job.delay_after(succeeded=False) == 60
    assert 9 <= job.delay_after(succeeded=True) <= 11


async def test_initial_delay_is_respected():
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    scheduler = Scheduler()
    scheduler.add("later", 5, job, initial_delay=10)
    stop = asyncio.Event()
    runner = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await runner
    assert calls == 0


async def test_stop_cancels_running_jobs():
    cancelled = asyncio.Event()

    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scheduler = Scheduler()
    job = scheduler.add("forever", 1, forever)
    await _run_until(scheduler, lambda: job.running)
    assert cancelled.is_set()


def test_duplicate_job_names_rejected():
    scheduler = Scheduler()

    async def noop():
        return None

    scheduler.add("a", 1, noop)
    with pytest.raises(ValueError, match="duplicate"):
        scheduler.add("a", 1, noop)
