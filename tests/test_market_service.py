from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FIXTURES, T0, make_snapshot
from tracker.db.models import EbayStats, Event, Listing, MarketComp, Valuation, Variant, Watch
from tracker.domain import EventKind, StockStatus
from tracker.ingest import Ingestor
from tracker.market.ebay import EbayClient, EbayError, EbayItem, parse_active, parse_sold
from tracker.market.service import MarketService, retail_listing, search_query
from tracker.settings import EbaySettings, MarketSettings


def _json(name: str) -> dict:
    return json.loads((FIXTURES / "ebay" / name).read_text(encoding="utf-8"))


# ── eBay client ───────────────────────────────────────────────────────────


def test_parse_active_fixture():
    items = parse_active(_json("browse_u7_pro.json"))
    assert len(items) == 5
    first = items[0]
    assert first.item_id == "v1|110551000001|0"
    assert first.price_cents == 26999
    assert first.shipping_cents == 0
    assert first.condition_id == "1000"
    assert not first.sold
    assert items[1].shipping_cents == 1250
    assert items[4].shipping_cents is None  # calculated shipping: unknown


def test_parse_sold_fixture():
    items = parse_sold(_json("sold_u7_pro.json"))
    assert [i.price_cents for i in items] == [23900, 24500, 6000]
    assert items[0].sold
    assert items[0].sold_at == datetime(2026, 11, 1, 18, 22, 31, tzinfo=UTC)
    assert items[1].sold_quantity == 2


def _ebay_transport(insights_status: int = 200) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        if request.url.path.endswith("item_summary/search"):
            return httpx.Response(200, json=_json("browse_u7_pro.json"))
        if request.url.path.endswith("item_sales/search"):
            if insights_status != 200:
                return httpx.Response(insights_status, json={"errors": []})
            return httpx.Response(200, json=_json("sold_u7_pro.json"))
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


def _ebay_settings() -> EbaySettings:
    return EbaySettings(_env_file=None, client_id="id", client_secret="secret")  # type: ignore[call-arg]


async def test_client_auth_headers_filters_and_token_reuse():
    transport, seen = _ebay_transport()
    client = EbayClient(_ebay_settings(), transport=transport)
    await client.search_active("ubiquiti U7 Pro", min_price_cents=5670)
    await client.search_active("ubiquiti U7 Pro")
    await client.aclose()

    token_calls = [r for r in seen if r.url.path.endswith("/token")]
    assert len(token_calls) == 1  # cached
    assert token_calls[0].headers["Authorization"] == "Basic aWQ6c2VjcmV0"
    search = seen[1]
    assert search.headers["Authorization"] == "Bearer tok"
    assert search.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert "price:[56.70..]" in search.url.params["filter"]
    assert "conditionIds:{1000|1500}" in search.url.params["filter"]
    assert "itemLocationCountry:US" in search.url.params["filter"]


async def test_item_location_filter_applies_to_sold_and_can_be_disabled():
    transport, seen = _ebay_transport()
    client = EbayClient(_ebay_settings(), transport=transport)
    await client.search_sold("q")
    await client.aclose()
    sold = next(r for r in seen if r.url.path.endswith("item_sales/search"))
    assert "itemLocationCountry:US" in sold.url.params["filter"]

    transport, seen = _ebay_transport()
    anywhere = EbaySettings(  # type: ignore[call-arg]
        _env_file=None, client_id="id", client_secret="secret", item_location_country=""
    )
    client = EbayClient(anywhere, transport=transport)
    await client.search_active("q")
    await client.aclose()
    search = next(r for r in seen if r.url.path.endswith("item_summary/search"))
    assert "itemLocationCountry" not in search.url.params["filter"]


async def test_insights_refusal_disables_sold_search():
    transport, seen = _ebay_transport(insights_status=403)
    client = EbayClient(_ebay_settings(), transport=transport)
    assert await client.search_sold("q") == []
    assert client.insights_enabled is False
    calls = len(seen)
    assert await client.search_sold("q") == []
    assert len(seen) == calls  # no further requests
    await client.aclose()


# ── service ───────────────────────────────────────────────────────────────


class FakeEbay:
    def __init__(self, active: list[EbayItem], sold: list[EbayItem]) -> None:
        self.active, self.sold = active, sold
        self.queries: list[tuple[str, int | None]] = []

    async def search_active(self, query: str, *, min_price_cents=None, limit=100):
        self.queries.append((query, min_price_cents))
        return self.active

    async def search_sold(self, query: str, *, min_price_cents=None, limit=100):
        return self.sold


class Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self) -> int:
        self.calls += 1
        return 0


SETTINGS = MarketSettings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
async def catalog(sessions: async_sessionmaker[AsyncSession]):
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                make_snapshot(),  # U7-Pro $189 in stock
                make_snapshot(external_id="U7-Pro-Max-US", sku="U7-Pro-Max", price_cents=27900),
                make_snapshot(
                    external_id="UACC-Cover-US", sku="UACC-Cover", price_cents=1500
                ),  # cheap accessory
                make_snapshot(
                    region="ca", external_id="U7-Pro-US", currency="CAD", price_cents=23900
                ),
            ],
            observed_at=T0,
        )
    return sessions


async def _variant_id(sessions, sku: str) -> int:
    async with sessions() as session:
        return await session.scalar(select(Variant.id).where(Variant.sku == sku))


def _service(sessions, ebay: FakeEbay | None, now: Callable[[], datetime] = lambda: T0):
    return MarketService(sessions, ebay, SETTINGS, Dispatcher(), clock=now)  # type: ignore[arg-type]


async def test_collect_matches_stores_and_values(catalog):
    ebay = FakeEbay(
        parse_active(_json("browse_u7_pro.json")), parse_sold(_json("sold_u7_pro.json"))
    )
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    service = _service(catalog, ebay, lambda: now)
    vid = await _variant_id(catalog, "U7-Pro")
    report = await service.collect(vid)

    assert ebay.queries == [("ubiquiti U7 Pro", 5670)]
    assert report.fetched == 8
    assert report.matched == 6  # 4 active (incl. a lot of 2) + 2 sold
    assert report.excluded == 2  # U7 Pro Max, for-parts
    # Exclusion reasons are visible on the live report...
    reasons = {m.reason for _, m in report.items if not m.matched}
    assert reasons == {"different model (u7-pro-max)", "parts or not working"}
    lot = next(m for _, m in report.items if m.quantity == 2)
    assert lot.condition == "open_box"

    async with catalog() as session:
        # ...but no eBay listing data is ever written (eBay "do not persist" compliance).
        assert (await session.scalars(select(MarketComp))).all() == []
        stats = await session.get(EbayStats, vid)
        assert (stats.matched, stats.excluded, stats.active_count) == (6, 2, 4)
        assert stats.fetched_at == now
        valuation = await session.get(Valuation, vid)
        variant = await session.get(Variant, vid)
        assert variant.comps_checked_at == now

    # 2 sold comps < min 3, so the basis is active listings (4 sane comps).
    assert valuation.basis == "active"
    assert valuation.active_count == 4
    assert valuation.retail_cents == 18900  # the US/USD listing, not CAD
    assert valuation.net_cents is not None


async def test_collect_twice_keeps_one_aggregate_row(catalog):
    ebay = FakeEbay(parse_active(_json("browse_u7_pro.json")), [])
    service = _service(catalog, ebay)
    vid = await _variant_id(catalog, "U7-Pro")
    await service.collect(vid)
    await service.collect(vid)
    async with catalog() as session:
        assert len((await session.scalars(select(EbayStats))).all()) == 1
        assert (await session.scalars(select(MarketComp))).all() == []


async def test_stale_active_stats_are_ignored(catalog):
    ebay = FakeEbay(parse_active(_json("browse_u7_pro.json")), [])
    vid = await _variant_id(catalog, "U7-Pro")
    await _service(catalog, ebay, lambda: T0).collect(vid)

    fresh = (await _service(catalog, None, lambda: T0 + timedelta(hours=95)).revalue([vid]))[vid]
    assert fresh.basis == "active"
    stale = (await _service(catalog, None, lambda: T0 + timedelta(hours=97)).revalue([vid]))[vid]
    assert stale.basis is None


async def test_ebay_sold_beats_manual_and_manual_beats_active(catalog):
    sold = [
        EbayItem(f"s{i}", "Ubiquiti U7 Pro new", None, p, 0, "USD", "1000", True, T0)
        for i, p in enumerate((30000, 30500, 31000))
    ]
    vid = await _variant_id(catalog, "U7-Pro")
    service = _service(catalog, FakeEbay(parse_active(_json("browse_u7_pro.json")), []))
    await service.collect(vid)
    for price in (26000, 26500, 27000):
        await service.add_manual_comp(vid, price)
    assert (await service.revalue([vid]))[vid].basis == "manual"  # manual sold > eBay active

    service = _service(catalog, FakeEbay([], sold))
    await service.collect(vid)
    result = (await service.revalue([vid]))[vid]
    assert result.basis == "sold"
    assert result.resale_cents == 30500
    assert result.manual_count == 3


async def test_manual_comps_create_deal_event_once(catalog):
    dispatcher = Dispatcher()
    service = MarketService(catalog, None, SETTINGS, dispatcher, clock=lambda: T0)
    vid = await _variant_id(catalog, "U7-Pro")
    for price in (30000, 31000, 32000):
        result = await service.add_manual_comp(vid, price, created_by="1")
    assert result.basis == "manual"
    assert result.net_cents > SETTINGS.deal_min_profit_cents

    await service.revalue_all()  # still a deal: no second event
    async with catalog() as session:
        events = (await session.scalars(select(Event))).all()
        assert [(e.kind, e.new_value) for e in events] == [
            (EventKind.DEAL, f"net ${result.net_cents / 100:,.2f} · ROI {result.roi:.0%}")
        ]
        valuation = await session.get(Valuation, vid)
        assert valuation.deal_active
    assert dispatcher.calls >= 1


async def test_deal_rearms_after_selling_out(catalog):
    service = _service(catalog, None)
    vid = await _variant_id(catalog, "U7-Pro")
    for price in (30000, 31000, 32000):
        await service.add_manual_comp(vid, price)
    async with catalog() as session, session.begin():
        listing = await session.scalar(
            select(Listing).where(Listing.variant_id == vid, Listing.region == "us")
        )
        listing.status = StockStatus.OUT_OF_STOCK.value
    await service.revalue_all()
    async with catalog() as session, session.begin():
        assert not (await session.get(Valuation, vid)).deal_active
        listing = await session.scalar(
            select(Listing).where(Listing.variant_id == vid, Listing.region == "us")
        )
        listing.status = StockStatus.IN_STOCK.value
    await service.revalue_all()
    async with catalog() as session:
        kinds = [e.kind for e in (await session.scalars(select(Event))).all()]
    assert kinds == [EventKind.DEAL, EventKind.DEAL]


async def test_due_variants_priorities(catalog):
    service = _service(catalog, FakeEbay([], []))
    pro, pro_max, cover = [
        await _variant_id(catalog, sku) for sku in ("U7-Pro", "U7-Pro-Max", "UACC-Cover")
    ]
    due = await service.due_variants(10)
    # Accessory under $40 skipped; never-checked sorted by price (most at stake first).
    assert due == [pro_max, pro]

    async with catalog() as session, session.begin():
        listing = await session.scalar(select(Listing).where(Listing.variant_id == cover))
        session.add(Watch(discord_user_id="1", listing_id=listing.id))
    assert (await service.due_variants(10))[0] == cover  # watched → hot → first

    async with catalog() as session, session.begin():
        for vid in (pro, pro_max, cover):
            (await session.get(Variant, vid)).comps_checked_at = T0
    assert await service.due_variants(10) == []
    later = _service(catalog, FakeEbay([], []), lambda: T0 + timedelta(hours=7))
    assert await later.due_variants(10) == [cover]  # hot refresh after 6h, others after 72h


async def test_collect_due_stops_on_rate_limit(catalog):
    class LimitedEbay(FakeEbay):
        async def search_active(self, query, *, min_price_cents=None, limit=100):
            raise EbayError("too many requests", 429)

    service = MarketService(
        catalog,
        LimitedEbay([], []),
        MarketSettings(_env_file=None, collect_per_tick=5),  # type: ignore[call-arg]
        Dispatcher(),
        clock=lambda: T0,
    )
    assert await service.collect_due() == 0


async def test_without_ebay_collection_is_disabled(catalog):
    service = _service(catalog, None)
    assert not service.collecting
    assert await service.collect_due() == 0
    with pytest.raises(EbayError):
        await service.collect(await _variant_id(catalog, "U7-Pro"))


async def test_helpers(catalog):
    assert search_query("U7-Pro", 1) == "ubiquiti U7 Pro"
    assert search_query("U7-Pro-5", 5) == "ubiquiti U7 Pro 5 pack"
    async with catalog() as session:
        listing = await retail_listing(session, await _variant_id(catalog, "U7-Pro"))
        assert (listing.region, listing.currency) == ("us", "USD")


# ── alerts, queries, and embeds with valuations ───────────────────────────


async def test_deal_and_back_in_stock_alerts_carry_profit(catalog):
    from tracker.alerts import AlertRouter
    from tracker.bot.embeds import alert_content, alert_embed
    from tracker.outbox import load_events
    from tracker.settings import DiscordSettings

    service = _service(catalog, None)
    vid = await _variant_id(catalog, "U7-Pro")
    for price in (30000, 31000, 32000):
        await service.add_manual_comp(vid, price)
    async with catalog() as session:
        [deal] = await load_events(session, pending_only=True, limit=10)
    assert deal.kind is EventKind.DEAL
    assert deal.net_cents is not None
    assert deal.valuation_basis == "manual"
    assert deal.comps_count == 3
    assert deal.profit_text.startswith("+$")
    assert deal.summary().startswith("DEAL · U7-Pro")

    settings = DiscordSettings(_env_file=None, channel_id=1)  # type: ignore[call-arg]
    async with catalog() as session:
        [plan] = await AlertRouter(settings).plan(session, [deal])
    assert plan.important
    assert plan.channel_id == 1
    assert "est." in alert_content(plan)
    fields = {f.name: f.value for f in alert_embed(deal).fields}
    assert "manual · 3 comps" in fields["💰 Resale estimate"]


async def test_opportunities_query_and_embeds(catalog):
    from tracker.bot.commands import _opportunity_line, value_embed
    from tracker.queries import opportunities, variant_comps

    service = _service(catalog, None)
    pro = await _variant_id(catalog, "U7-Pro")
    pro_max = await _variant_id(catalog, "U7-Pro-Max")
    for price in (30000, 31000, 32000):
        await service.add_manual_comp(pro, price)
    for price in (29000, 29500, 30000):  # thin margin on a $279 item
        await service.add_manual_comp(pro_max, price)

    async with catalog() as session:
        rows = await opportunities(session)
        assert [r.variant.sku for r in rows] == ["U7-Pro", "U7-Pro-Max"]
        assert rows[1].valuation.net_cents < 0
        assert [r.variant.sku for r in await opportunities(session, min_profit_cents=1)] == [
            "U7-Pro"
        ]
        assert await opportunities(session, category="switching") == []
        line = _opportunity_line(1, rows[0])
        assert "**U7-Pro**" in line
        assert "buy $189.00" in line

        variant = await session.get(Variant, pro)
        comps = await variant_comps(session, pro)
        embed = value_embed(variant, None, rows[0].valuation, comps, SETTINGS)
    assert "Buy **$189.00** (in stock)" in embed.description
    breakdown = embed.fields[0].value
    assert breakdown.startswith("Resale $310.00")
    assert len(embed.fields[2].value.splitlines()) == 3


def test_value_embed_without_valuation():
    from tracker.bot.commands import value_embed

    variant = Variant(sku="E7", sku_key="e7", pack_size=1)
    embed = value_embed(variant, None, None, [], SETTINGS)
    assert "Not enough comps" in embed.description
