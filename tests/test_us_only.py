"""US only: other stores' data stays out of the screens, and US needs no label."""

from __future__ import annotations

from types import SimpleNamespace

from tests.test_screens import OWNER, bot, controls, press, variant_id, viewer  # noqa: F401
from tracker.bot import screens
from tracker.bot.embeds import place_name, unreadable
from tracker.bot.render import value_embed
from tracker.permissions import Role
from tracker.settings import MarketSettings


def test_only_other_stores_get_a_region_label() -> None:
    assert place_name("unifi", "us") == "Ubiquiti Store"
    assert place_name("unifi", "ca") == "Ubiquiti Store CA"


def test_a_page_that_cant_be_read_says_so() -> None:
    blocked = SimpleNamespace(source="bestbuy", consecutive_unknowns=691)
    assert "Best Buy API key" in (unreadable(blocked) or "")
    amazon = SimpleNamespace(source="amazon", consecutive_unknowns=263)
    assert unreadable(amazon) == "⚠️ Can't read the store page (263 tries)"
    assert unreadable(SimpleNamespace(source="amazon", consecutive_unknowns=2)) is None


async def test_us_only_hides_other_stores(bot) -> None:  # noqa: F811
    bot.ctx.settings.unifi.regions = ["us"]
    v = await viewer(bot, OWNER, Role.OWNER)
    product = await screens.product_screen(v, f"p|{await variant_id(bot)}|home")
    assert [f.name for f in product.embed.fields if "Recent" not in f.name] == ["Ubiquiti Store"]

    radar = await screens.radar_screen(v)
    assert "Region…" not in radar.labels()
    shows = next(c for c in controls(radar.view()) if c.placeholder == "What to show…")
    assert [o.label for o in shows.options][:2] == ["Important changes", "Everything"]
    assert radar.embed.title == "Radar · Important changes"

    watches = await screens.watches_screen(v)
    every = controls(watches.view())[0]
    assert [o.label for o in every.options] == [
        "Every important alert: Off",
        "Every important alert: On",
    ]

    health = await screens.health_screen(v)
    assert not any(f.name.startswith("CA") for f in health.embed.fields)


async def test_two_regions_still_offer_the_region_menu(bot) -> None:  # noqa: F811
    v = await viewer(bot, OWNER, Role.OWNER)
    radar = await screens.radar_screen(v)
    assert "Region…" in radar.labels()


def test_profit_shows_the_estimate_flips_ranks_by() -> None:
    valuation = SimpleNamespace(
        basis=None,
        flip_basis="scarcity",
        retail_cents=399900,
        available=True,
        scarcity_resale_cents=599999,
        scarcity_days=2,
        flip_net_cents=117259,
        flip_roi=0.29,
        flip_confidence="low",
    )
    variant = SimpleNamespace(sku="ECS-48S-PoE")
    embed = value_embed(variant, None, valuation, [], MarketSettings(_env_file=None))  # type: ignore[arg-type,call-arg]
    text = embed.description or ""
    assert "$1,172.59" in text
    assert "sold out 2d" in text
    assert "Not enough comps" not in text


async def test_snatch_watches_the_us_store_and_has_a_limit(bot) -> None:  # noqa: F811
    from sqlalchemy import select

    from tracker.db.models import Listing, Watch

    vid = await variant_id(bot)
    state = f"p|{vid}|home"
    pressed = await press(bot, "snatch", state, user_id=OWNER)
    assert "snatch list" in pressed.shown["content"]
    assert "⚡ Snatch: on" in [getattr(c, "label", None) for c in controls(pressed.shown["view"])]
    async with bot.ctx.sessions() as session:
        listings = list(await session.scalars(select(Listing).where(Listing.variant_id == vid)))
        watched = set(await session.scalars(select(Watch.listing_id)))
    us = [x for x in listings if x.region == "us"]
    assert all(x.snatch for x in us)
    assert not any(x.snatch for x in listings if x.region != "us")
    assert {x.id for x in us} <= watched  # pinged without having to watch it first

    member = await press(bot, "snatch", state)
    assert "Only the bot owner" in member.response.sent[0]["content"]

    async with bot.ctx.sessions() as session, session.begin():
        for listing in await session.scalars(select(Listing)):
            listing.snatch = True  # pretend the list is full (several products)
    from tracker.services import fastlane

    original = fastlane.SNATCH_LIMIT
    try:
        import tracker.bot.screens as screens_module

        screens_module.SNATCH_LIMIT = 1
        other = await variant_id(bot, "UDM-SE")
        async with bot.ctx.sessions() as session, session.begin():
            for listing in await session.scalars(
                select(Listing).where(Listing.variant_id == other)
            ):
                listing.snatch = False
        full = await press(bot, "snatch", f"p|{other}|home", user_id=OWNER)
        assert "Take one off first" in full.response.sent[0]["content"]
    finally:
        screens_module.SNATCH_LIMIT = original
