"""Owner panel watchlist: add, edit, mute, bulk actions, priority, subscription, and rules."""

from __future__ import annotations

from sqlalchemy import select

from tests.conftest import T0, make_snapshot
from tests.test_panel import WRITE, Clock, audit_actions, login, make_client, panel_settings
from tracker.db.models import Listing, Watch, WatchRule
from tracker.ingest import Ingestor
from tracker.panel.watchlist import category_groups
from tracker.settings import DiscordSettings

OWNER = "123456789012345670"


def owner_settings():
    settings = panel_settings()
    settings.discord = DiscordSettings(_env_file=None, owner_ids=OWNER)  # type: ignore[call-arg]
    return settings


async def seed(sessions) -> None:
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                make_snapshot(),
                make_snapshot(
                    region="ca",
                    external_id="U7-Pro-CA",
                    url="https://store.ui.com/ca/en/products/u7-pro",
                    currency="CAD",
                    price_cents=26900,
                ),
            ],
            observed_at=T0,
        )


async def test_needs_an_owner_discord_id(sessions):
    await seed(sessions)
    clock = Clock()
    async with make_client(sessions, clock) as c:  # no DISCORD_OWNER_IDS
        await login(c, clock)
        listing = await c.get("/api/watchlist")
        assert listing.json()["owner_configured"] is False
        added = await c.post("/api/watchlist", json={"query": "U7-Pro"}, headers=WRITE)
        assert added.status_code == 409


async def test_add_edit_mute_remove_watch(sessions):
    await seed(sessions)
    clock = Clock()
    async with make_client(sessions, clock, owner_settings()) as c:
        await login(c, clock)
        added = await c.post(
            "/api/watchlist",
            json={"query": "u7-pro", "region": "us", "max_price_cents": 17000, "kinds": ["deal"]},
            headers=WRITE,
        )
        assert added.json() == {"listings": 1, "created": 1}
        assert (
            await c.post("/api/watchlist", json={"query": "zzz"}, headers=WRITE)
        ).status_code == 404
        bad = await c.post(
            "/api/watchlist", json={"query": "u7-pro", "kinds": ["x"]}, headers=WRITE
        )
        assert bad.status_code == 422

        [item] = (await c.get("/api/watchlist")).json()["items"]
        assert item["owner"] is True
        assert item["listing"]["region"] == "us"
        assert item["max_price_cents"] == 17000
        assert item["kinds"] == ["deal"]
        wid = item["watch_id"]

        edit = await c.patch(
            f"/api/watches/{wid}",
            json={"max_price_cents": None, "kinds": None, "mute_hours": 24},
            headers=WRITE,
        )
        assert edit.status_code == 200
        [item] = (await c.get("/api/watchlist")).json()["items"]
        assert item["max_price_cents"] is None
        assert item["kinds"] is None  # back to server defaults
        assert item["muted_until"] is not None
        await c.patch(f"/api/watches/{wid}", json={"mute_hours": 0}, headers=WRITE)
        assert (await c.get("/api/watchlist")).json()["items"][0]["muted_until"] is None

        assert (await c.delete(f"/api/watches/{wid}", headers=WRITE)).status_code == 200
        assert (await c.get("/api/watchlist")).json()["items"] == []
    actions = await audit_actions(sessions)
    assert {"watch.add", "watch.update", "watch.remove"} <= set(actions)


async def test_members_watches_can_be_removed_not_edited(sessions):
    await seed(sessions)
    async with sessions() as session, session.begin():
        listing_id = await session.scalar(select(Listing.id).where(Listing.region == "us"))
        session.add(Watch(discord_user_id="555", listing_id=listing_id, guild_id="1"))
    clock = Clock()
    async with make_client(sessions, clock, owner_settings()) as c:
        await login(c, clock)
        [item] = (await c.get("/api/watchlist")).json()["items"]
        assert item["owner"] is False
        wid = item["watch_id"]
        edit = await c.patch(f"/api/watches/{wid}", json={"mute_hours": 1}, headers=WRITE)
        assert edit.status_code == 403
        bulk = await c.post(
            "/api/watches/bulk", json={"ids": [wid], "action": "mute"}, headers=WRITE
        )
        assert bulk.json()["count"] == 0  # bulk mute only touches your own
        assert (await c.delete(f"/api/watches/{wid}", headers=WRITE)).status_code == 200


async def test_bulk_priority_and_subscription(sessions):
    await seed(sessions)
    clock = Clock()
    async with make_client(sessions, clock, owner_settings()) as c:
        await login(c, clock)
        await c.post("/api/watchlist", json={"query": "u7-pro"}, headers=WRITE)  # both regions
        items = (await c.get("/api/watchlist")).json()["items"]
        assert len(items) == 2
        ids = [i["watch_id"] for i in items]
        muted = await c.post(
            "/api/watches/bulk", json={"ids": ids, "action": "mute", "hours": 2}, headers=WRITE
        )
        assert muted.json()["count"] == 2
        listing_id = items[0]["listing"]["id"]
        prio = await c.post(
            f"/api/listings/{listing_id}/priority", json={"priority": "high"}, headers=WRITE
        )
        assert prio.status_code == 200
        async with sessions() as session:
            assert (await session.get(Listing, listing_id)).priority == "high"
        bad = await c.post(
            f"/api/listings/{listing_id}/priority", json={"priority": "max"}, headers=WRITE
        )
        assert bad.status_code == 422

        sub = await c.post("/api/subscription", json={"subscribed": True}, headers=WRITE)
        assert sub.json()["changed"] is True
        assert (await c.get("/api/watchlist")).json()["subscribed"] is True

        removed = await c.post(
            "/api/watches/bulk", json={"ids": ids, "action": "remove"}, headers=WRITE
        )
        assert removed.json()["count"] == 2


async def test_rules_crud_and_preview(sessions):
    await seed(sessions)
    clock = Clock()
    async with make_client(sessions, clock, owner_settings()) as c:
        await login(c, clock)
        empty = await c.post("/api/rules", json={"name": "everything"}, headers=WRITE)
        assert empty.status_code == 422  # a rule must match something specific

        preview = (await c.get("/api/rules/preview?query=u7&regions=ca")).json()
        assert preview["count"] == 1
        assert preview["sample"] == ["U7-Pro · Access Point U7 Pro"]

        created = await c.post(
            "/api/rules",
            json={
                "name": "U7 deals",
                "query": " u7 ",
                "regions": ["US", "ca"],
                "kinds": ["deal", "back_in_stock"],
                "min_profit_cents": 3000,
            },
            headers=WRITE,
        )
        assert created.status_code == 200, created.text
        rule = created.json()
        assert rule["query"] == "u7"
        assert rule["regions"] == ["ca", "us"]
        assert rule["kinds"] == ["back_in_stock", "deal"]
        bad_region = await c.post(
            "/api/rules", json={"name": "x", "regions": ["us; drop"]}, headers=WRITE
        )
        assert bad_region.status_code == 422

        off = await c.patch(
            f"/api/rules/{rule['id']}", json={"enabled": False, "mute_hours": 5}, headers=WRITE
        )
        assert off.json()["enabled"] is False
        assert off.json()["muted_until"] is not None
        listed = (await c.get("/api/watchlist")).json()["rules"]
        assert [r["name"] for r in listed] == ["U7 deals"]
        assert (await c.delete(f"/api/rules/{rule['id']}", headers=WRITE)).status_code == 200
        async with sessions() as session:
            assert await session.scalar(select(WatchRule.id)) is None
    actions = await audit_actions(sessions)
    assert {"rule.add", "rule.update", "rule.remove"} <= set(actions)


async def test_watchlist_needs_login(sessions):
    clock = Clock()
    async with make_client(sessions, clock, owner_settings()) as c:
        assert (await c.get("/api/watchlist")).status_code == 401
        no_header = await c.post("/api/rules", json={"name": "x", "query": "u7"})
        assert no_header.status_code == 403


async def test_owner_learned_from_the_bot(sessions):
    """No DISCORD_OWNER_IDS: the panel uses the owner the bot learned from Discord."""
    await seed(sessions)
    clock = Clock()
    async with make_client(sessions, clock, owner_ids=lambda: {"777"}) as c:
        await login(c, clock)
        assert (await c.get("/api/watchlist")).json()["owner_configured"] is True
        assert (
            await c.post("/api/watchlist", json={"query": "u7-pro"}, headers=WRITE)
        ).status_code == 200
    async with sessions() as session:
        assert set(await session.scalars(select(Watch.discord_user_id))) == {"777"}


def test_category_groups():
    counts = {
        "physical-security-dome-turret": 17,
        "physical-security-bullet": 8,
        "wifi-flagship": 10,
        "wifi-outdoor": 8,
        "network-storage": 6,
    }
    groups = {g["value"]: g for g in category_groups(counts)}
    assert set(groups) == {"physical-security", "wifi"}  # single-category families are skipped
    assert groups["physical-security"]["count"] == 25
    assert groups["physical-security"]["label"] == "Physical security (all 2)"


async def test_categories_endpoint(sessions):
    await seed(sessions)
    clock = Clock()
    async with make_client(sessions, clock, owner_settings()) as c:
        await login(c, clock)
        data = (await c.get("/api/categories")).json()
    assert data["categories"] == [{"value": "wifi-flagship", "label": "Wifi flagship", "count": 1}]
