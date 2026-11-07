from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tracker.http import HttpClient
from tracker.settings import HttpSettings, UnifiSettings
from tracker.sources.unifi import UnifiParseError, UnifiStore


def _client(routes: dict[str, str], seen: list[str]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url in routes:
            return httpx.Response(200, text=routes[url])
        return httpx.Response(404)

    settings = HttpSettings(  # type: ignore[call-arg]
        _env_file=None, max_retries=0, per_host_min_interval_seconds=0
    )
    return HttpClient(settings, transport=httpx.MockTransport(handler))


def _store(http: HttpClient) -> UnifiStore:
    return UnifiStore(http, UnifiSettings(_env_file=None))  # type: ignore[call-arg]


async def test_regional_store_uses_regional_origin(fixture_text: Callable[[str], str]):
    seen: list[str] = []
    url = "https://ca.store.ui.com/ca/en/products/u7-pro"
    async with _client({url: fixture_text("unifi/product_u7-pro_ca.html")}, seen) as http:
        snapshots = await _store(http).fetch_product("u7-pro", "ca")
    assert seen == [url]
    assert {s.currency for s in snapshots} == {"CAD"}


async def test_unsupported_region_served_us_store_is_rejected(
    fixture_text: Callable[[str], str],
):
    # The live store answers /au/ by redirecting to the US store; the client must notice.
    seen: list[str] = []
    url = "https://au.store.ui.com/au/en/products/u7-pro"
    async with _client({url: fixture_text("unifi/product_u7-pro.html")}, seen) as http:
        with pytest.raises(UnifiParseError, match="unsupported region"):
            await _store(http).fetch_product("u7-pro", "au")


async def test_crawl_uses_home_links_and_skips_covered_subcategories(
    fixture_text: Callable[[str], str],
):
    seen: list[str] = []
    base = "https://store.ui.com/us/en"
    routes = {
        base: fixture_text("unifi/home.html"),
        f"{base}/category/all-wifi": fixture_text("unifi/category_all-wifi.html"),
    }
    async with _client(routes, seen) as http:
        result = await _store(http).crawl_catalog("us")

    # all-wifi embeds wifi-whole-home-wifi, so that linked slug is never fetched.
    assert f"{base}/category/wifi-whole-home-wifi" not in seen
    assert "all-wifi" in result.categories_crawled
    assert result.snapshots
    # Every other linked category 404s in this test, so the crawl is incomplete.
    assert not result.complete
    assert "all-switching" in result.errors
