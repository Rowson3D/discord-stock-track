"""Minimal asyncio job scheduler.

* Each job runs at most once at a time (single-flight); a slow crawl never stacks up.
* Jobs run concurrently with each other, so a 10-second crawl doesn't delay hot polls.
* Intervals get ±jitter so requests don't line up into bursts.
* Failures back off exponentially (capped) and recover to the normal interval on success.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Job:
    name: str
    interval: float
    func: Callable[[], Awaitable[object]]
    jitter: float = 0.1
    max_backoff: float = 1800.0
    next_run: float = 0.0
    failures: int = 0
    runs: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def delay_after(self, succeeded: bool) -> float:
        if succeeded:
            spread = self.interval * self.jitter
            return max(0.0, self.interval + random.uniform(-spread, spread))
        return min(
            self.interval * (2 ** min(self.failures, 16)), max(self.max_backoff, self.interval)
        )


class Scheduler:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._wake = asyncio.Event()

    @property
    def jobs(self) -> dict[str, Job]:
        return self._jobs

    def add(
        self,
        name: str,
        interval: float,
        func: Callable[[], Awaitable[object]],
        *,
        initial_delay: float = 0.0,
        jitter: float = 0.1,
        max_backoff: float = 1800.0,
    ) -> Job:
        if name in self._jobs:
            raise ValueError(f"duplicate job {name!r}")
        job = Job(
            name=name,
            interval=interval,
            func=func,
            jitter=jitter,
            max_backoff=max_backoff,
            next_run=self._clock() + initial_delay,
        )
        self._jobs[name] = job
        self._wake.set()
        return job

    async def _execute(self, job: Job) -> None:
        succeeded = False
        try:
            await job.func()
            succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception:
            job.failures += 1
            logger.exception("Job %s failed (%d consecutive)", job.name, job.failures)
        finally:
            job.runs += 1
            if succeeded:
                if job.failures:
                    logger.info("Job %s recovered after %d failure(s)", job.name, job.failures)
                job.failures = 0
            job.next_run = self._clock() + job.delay_after(succeeded)
            self._wake.set()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        try:
            while not stop.is_set():
                # Clear before inspecting jobs: a job finishing after this point re-sets it.
                self._wake.clear()
                now = self._clock()
                for job in self._jobs.values():
                    if not job.running and job.next_run <= now:
                        job.task = asyncio.create_task(self._execute(job), name=f"job:{job.name}")

                idle = [j.next_run for j in self._jobs.values() if not j.running]
                timeout = max(0.0, min(idle) - self._clock()) if idle else None
                waiters = [
                    asyncio.ensure_future(self._wake.wait()),
                    asyncio.ensure_future(stop.wait()),
                ]
                try:
                    await asyncio.wait(
                        waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for waiter in waiters:
                        waiter.cancel()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.running and job.task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
