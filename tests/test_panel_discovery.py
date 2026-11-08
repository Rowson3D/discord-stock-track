"""Owner panel Discovery inbox: check an item, list, detail, recheck, track, ignore."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from tests.conftest import T0, make_snapshot
from tests.test_discovery import MARKET, SETTINGS, Clock, FakeEbay, sold_out_market
from tests.test_panel import WRITE, audit_actions, login, make_client
from tests.test_panel import Clock as PanelClock
from tracker.db.models import Listing, Watch
from tracker.discovery.service import DiscoveryService, Signal
from tracker.ingest import Ingestor
from tracker.watching import Resolved

OWNER = "777"


async def test_discovery_off(sessions):
    clock = PanelClock()
    async with make_client(sessions, clock) as c:
        await login(c, clock)
        assert (await c.get("/api/discovery")).json()["enabled"] is False
        check = await c.post("/api/discovery/check", json={"text": "PS5 Pro"}, headers=WRITE)
        assert check.status_code == 409


async def test_check_list_detail_recheck(sessions):
    discovery = DiscoveryService(
        sessions, FakeEbay(sold_out_market()), MARKET, SETTINGS, clock=Clock()
    )
    clock = PanelClock()
    async with make_client(sessions, clock, discovery=discovery) as c:
        await login(c, clock)
        checked = await c.post(
            "/api/discovery/check",
            json={"text": "Sony WH-1000XM5 Wireless Headphones"},
            headers=WRITE,
        )
        assert checked.status_code == 200, checked.text
        cid = checked.json()["id"]

        inbox = (await c.get("/api/discovery?status=all")).json()
        [item] = inbox["items"]
        assert item["model"] == "WH-1000XM5"
        assert item["ebay_sellers"] == 5
        assert item["net_cents"] is None  # no store price yet: market only
        assert inbox["budget"] == {"used": 1, "limit": SETTINGS.daily_ebay_calls}
        assert inbox["sources"] == {"ebay": True, "bestbuy": False, "bestbuy_categories": 0}
        assert (await c.get("/api/discovery?status=bogus")).status_code == 422

        detail = (await c.get(f"/api/discovery/{cid}")).json()
        assert [s["kind"] for s in detail["signals"]] == ["manual"]
        assert len(detail["history"]) == 1
        assert (await c.get("/api/discovery/99999")).status_code == 404

        again = await c.post(f"/api/discovery/{cid}/recheck", headers=WRITE)
        assert again.status_code == 200
        bad = await c.post("/api/discovery/check", json={"text": "https://example.com/x"},
                           headers=WRITE)  # fmt: skip
        assert bad.status_code == 422
    assert {"discovery.check", "discovery.recheck"} <= set(await audit_actions(sessions))


async def test_track_adds_store_link_to_watchlist_and_ignore(sessions):
    discovery = DiscoveryService(
        sessions, FakeEbay(sold_out_market()), MARKET, SETTINGS, clock=Clock()
    )
    await discovery.ingest(
        [
            Signal(source="bestbuy", kind="sold_out", title="Sony WH-1000XM5 Headphones",
                   price_cents=29999, available=False, retail_source="bestbuy",
                   url="https://www.bestbuy.com/site/1.p"),
            Signal(source="manual", kind="manual", title="Some other gadget XZ-900"),
        ]
    )  # fmt: skip
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest([make_snapshot()], observed_at=T0)
    async with sessions() as session:
        listing_id = await session.scalar(select(Listing.id))
    resolved_urls: list[str] = []

    async def resolve(text: str, region: str | None) -> Resolved:
        resolved_urls.append(text)
        listing = SimpleNamespace(id=listing_id)
        return Resolved([SimpleNamespace(listing=listing, variant=SimpleNamespace(sku="x"))])  # type: ignore[list-item]

    clock = PanelClock()
    async with make_client(
        sessions, clock, discovery=discovery, resolve=resolve, owner_ids=lambda: {OWNER}
    ) as c:
        await login(c, clock)
        items = (await c.get("/api/discovery?status=new")).json()["items"]
        sony = next(i for i in items if i["retail_url"])
        other = next(i for i in items if not i["retail_url"])

        tracked = await c.post(
            f"/api/discovery/{sony['id']}/decision", json={"action": "track"}, headers=WRITE
        )
        assert tracked.json() == {"id": sony["id"], "status": "tracked", "watched": 1, "note": None}
        assert resolved_urls == ["https://www.bestbuy.com/site/1.p"]

        ignored = await c.post(
            f"/api/discovery/{other['id']}/decision", json={"action": "ignore"}, headers=WRITE
        )
        assert ignored.json()["status"] == "ignored"
        counts = (await c.get("/api/discovery")).json()["counts"]
        assert counts["tracked"] == 1
        assert counts["ignored"] == 1
    async with sessions() as session:
        watch = await session.scalar(select(Watch))
    assert watch.discord_user_id == OWNER
    assert watch.listing_id == listing_id
