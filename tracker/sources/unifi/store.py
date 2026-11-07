"""Ubiquiti store client: fetch products, categories, and crawl the full catalog over plain HTTP."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from tracker.domain import ListingSnapshot
from tracker.http import FetchError, HttpClient
from tracker.settings import UnifiSettings
from tracker.sources.unifi.parser import (
    CategoryPage,
    HomePage,
    UnifiParseError,
    dedupe,
    extract_next_data,
    is_category_page,
    page_region,
    parse_category_page,
    parse_home_page,
    parse_product_page,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CrawlResult:
    region: str
    snapshots: list[ListingSnapshot] = field(default_factory=list)
    categories_crawled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # Linked slugs that aren't category pages.
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Every discovered category loaded; safe to infer that missing listings are gone."""
        return not self.errors and bool(self.snapshots)


class UnifiStore:
    def __init__(self, http: HttpClient, settings: UnifiSettings) -> None:
        self._http = http
        self._settings = settings

    def origin(self, region: str) -> str:
        return self._settings.origin(region)

    def _url(self, region: str, path: str = "") -> str:
        return f"{self.origin(region)}/{region}/{self._settings.language}{path}"

    @staticmethod
    def _check_region(data: dict[str, Any], region: str) -> None:
        served = page_region(data)
        if served != region:
            raise UnifiParseError(
                f"asked for the {region!r} store but got {served!r} (unsupported region?)"
            )

    async def fetch_product(self, slug: str, region: str = "us") -> list[ListingSnapshot]:
        data = extract_next_data(await self._http.get_text(self._url(region, f"/products/{slug}")))
        self._check_region(data, region)
        return parse_product_page(data, base_url=self.origin(region))

    async def fetch_category(self, slug: str, region: str = "us") -> CategoryPage | None:
        """None when the slug isn't a real category page (the store redirected elsewhere)."""
        data = extract_next_data(await self._http.get_text(self._url(region, f"/category/{slug}")))
        self._check_region(data, region)
        if not is_category_page(data):
            return None
        return parse_category_page(data, base_url=self.origin(region))

    async def fetch_home(self, region: str = "us") -> HomePage:
        home = parse_home_page(await self._http.get_text(self._url(region)))
        if home.region != region:
            raise UnifiParseError(
                f"asked for the {region!r} store but got {home.region!r} (unsupported region?)"
            )
        return home

    async def crawl_catalog(self, region: str = "us") -> CrawlResult:
        """Crawl every category once.

        Top-level `all-*` categories are crawled first; they embed their subcategories, so
        any linked slug already seen as a subcategory is skipped. Whatever remains (e.g.
        accessories, storage) is crawled afterwards.
        """
        result = CrawlResult(region=region)
        discovered = (await self.fetch_home(region)).category_slugs
        for extra in self._settings.extra_categories:
            if extra not in discovered:
                discovered.append(extra)
        if not discovered:
            raise UnifiParseError(f"no category links found on the {region} store home page")

        covered: set[str] = set()
        top_level = [slug for slug in discovered if slug.startswith("all-")]
        remaining = [slug for slug in discovered if not slug.startswith("all-")]
        collected: list[ListingSnapshot] = []

        for batch in (top_level, remaining):
            for slug in batch:
                if slug in covered:
                    continue
                try:
                    page = await self.fetch_category(slug, region)
                except (FetchError, UnifiParseError) as exc:
                    logger.warning("UniFi category %s/%s failed: %s", region, slug, exc)
                    result.errors[slug] = str(exc)
                    continue
                covered.add(slug)
                if page is None:
                    result.skipped.append(slug)
                    continue
                covered.update(page.subcategory_ids)
                result.categories_crawled.append(slug)
                collected.extend(page.snapshots)

        result.snapshots = dedupe(collected)
        return result
