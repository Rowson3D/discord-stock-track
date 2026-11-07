from __future__ import annotations

import httpx
import pytest

from tracker.http import FetchError, HttpClient
from tracker.settings import HttpSettings


def _settings(**overrides: object) -> HttpSettings:
    values: dict[str, object] = {
        "max_retries": 2,
        "retry_backoff_seconds": 0,
        "per_host_min_interval_seconds": 0,
    }
    values.update(overrides)
    return HttpSettings(_env_file=None, **values)  # type: ignore[call-arg]


async def test_retries_transient_failures_then_succeeds():
    responses = iter([httpx.Response(503), httpx.Response(429), httpx.Response(200, text="ok")])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return next(responses)

    async with HttpClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        assert await client.get_text("https://store.ui.com/us/en") == "ok"
    assert calls == 3


async def test_non_retryable_status_fails_immediately():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with HttpClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as info:
            await client.get_text("https://store.ui.com/missing")
    assert calls == 1
    assert info.value.status == 404


async def test_gives_up_after_max_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with HttpClient(
        _settings(max_retries=1), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(FetchError, match="transport error"):
            await client.get_text("https://store.ui.com/")


async def test_sends_configured_user_agent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, text="")

    settings = _settings(user_agent="tracker-test/1.0")
    async with HttpClient(settings, transport=httpx.MockTransport(handler)) as client:
        await client.get_text("https://store.ui.com/")
    assert seen == ["tracker-test/1.0"]
