"""Store fetches only go to public web servers, redirects included."""

from __future__ import annotations

import httpx
import pytest

from tracker.http import FetchError, refuse_private_hosts


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api/session",  # the owner panel
        "http://192.168.1.1/",
        "http://10.0.0.5/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8765/",
        "http://printer.local/",
        "http://[::1]/",
    ],
)
async def test_private_and_local_addresses_are_refused(url: str) -> None:
    with pytest.raises(FetchError):
        await refuse_private_hosts(httpx.Request("GET", url))


async def test_public_addresses_are_allowed() -> None:
    await refuse_private_hosts(httpx.Request("GET", "https://8.8.8.8/"))
