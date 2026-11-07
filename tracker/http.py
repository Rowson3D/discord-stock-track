"""Shared async HTTP client with per-host politeness and retries."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from tracker.settings import HttpSettings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{reason} ({url})")
        self.url = url
        self.reason = reason
        self.status = status


class _HostGate:
    """Limits concurrency and enforces a minimum spacing between requests to one host."""

    def __init__(self, concurrency: int, min_interval: float) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._min_interval = min_interval
        self._next_allowed = 0.0

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()
        try:
            async with self._lock:
                wait = self._next_allowed - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_allowed = time.monotonic() + self._min_interval
        except BaseException:
            self._semaphore.release()
            raise

    async def __aexit__(self, *_: object) -> None:
        self._semaphore.release()


class HttpClient:
    def __init__(
        self, settings: HttpSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            http2=transport is None,
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._gates: dict[str, _HostGate] = {}

    def _gate(self, url: str) -> _HostGate:
        host = urlsplit(url).netloc.lower()
        gate = self._gates.get(host)
        if gate is None:
            gate = _HostGate(
                self._settings.per_host_concurrency, self._settings.per_host_min_interval_seconds
            )
            self._gates[host] = gate
        return gate

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 120.0)
        base = self._settings.retry_backoff_seconds * (2**attempt)
        return base + random.uniform(0, base / 2) if base else 0.0

    async def get_text(self, url: str) -> str:
        attempts = self._settings.max_retries + 1
        last_reason = "no attempts made"
        last_status: int | None = None

        for attempt in range(attempts):
            retry_after: str | None = None
            try:
                async with self._gate(url):
                    response = await self._client.get(url)
            except httpx.TransportError as exc:
                last_reason, last_status = f"transport error: {exc!r}", None
            else:
                if response.is_success:
                    return response.text
                last_status = response.status_code
                last_reason = f"HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE_STATUS:
                    raise FetchError(url, last_reason, last_status)
                retry_after = response.headers.get("Retry-After")

            if attempt < attempts - 1:
                delay = self._backoff(attempt, retry_after)
                logger.info(
                    "GET %s failed (%s); retry %d in %.1fs", url, last_reason, attempt + 1, delay
                )
                await asyncio.sleep(delay)

        raise FetchError(url, last_reason, last_status)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
