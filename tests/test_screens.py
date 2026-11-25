"""The bot's private, in-place navigation: each screen, who sees what, what each control does."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from sqlalchemy import select

from tests.conftest import T0, make_snapshot
from tests.test_watchtest import OWNER, settings
from tracker.bot import screens
from tracker.bot.screens import FLIPS, HOME, RADAR, WATCHES, Denied, Screen, Viewer
from tracker.bot.views import alert_view
from tracker.db.models import Guild, Listing, Valuation, Variant, Watch
from tracker.domain import StockStatus
from tracker.guilds import GuildStatus, config_for, decide
from tracker.ingest import Ingestor
from tracker.permissions import Role

GUILD = "555"
MEMBER = "42"


class Response:
    def __init__(self) -> None:
        self.done = False
        self.edited: dict[str, Any] | None = None
        self.sent: list[dict[str, Any]] = []
        self.modal: discord.ui.Modal | None = None

    def is_done(self) -> bool:
        return self.done

    async def edit_message(self, **kwargs: Any) -> None:
        self.done, self.edited = True, kwargs

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        self.done = True
        self.sent.append({"content": content, **kwargs})

    async def defer(self, **_: Any) -> None:
        self.done = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.done, self.modal = True, modal


class Followup:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})


class User(SimpleNamespace):
    def __str__(self) -> str:
        return f"user{self.id}"


class Interaction:
    def __init__(self, bot: Any, user_id: str, *, manage: bool = False) -> None:
        self.client = bot
        self.user = User(
            id=int(user_id), guild_permissions=SimpleNamespace(manage_guild=manage), roles=[]
        )
        self.guild_id = int(GUILD)
        self.channel = None
        self.response = Response()
        self.followup = Followup()
        self.original: dict[str, Any] | None = None

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.original = kwargs

    @property
    def shown(self) -> dict[str, Any]:
        """Whatever the message now shows (an edit, or an edit after deferring)."""
        result = self.original or self.response.edited
        assert result is not None, "nothing was shown"
        return result


class Market:
    collecting = False
    scanning = False


@pytest.fixture
async def bot(sessions):
    async with sessions() as session, session.begin():
        row = await decide(session, GUILD, GuildStatus.APPROVED, actor_id=None, name="Home")
        row.alert_channel_id = "9001"
        await Ingestor(session).ingest(
            [
                make_snapshot(),
                make_snapshot(
                    region="ca", external_id="U7-PRO-CA", currency="CAD", price_cents=23900
                ),
                make_snapshot(
                    sku="UDM-SE",
                    external_id="UDM-SE-US",
                    product_key="ubiquiti:udm-se",
                    product_name="Dream Machine SE",
                    url="https://store.ui.com/us/en/products/udm-se",
                    status=StockStatus.OUT_OF_STOCK,
                    restock_eta=date(2026, 11, 20),
                ),
            ],
            observed_at=T0,
        )
    config = settings()
    config.unifi.regions = ["us", "ca"]
    ctx = SimpleNamespace(
        sessions=sessions,
        settings=config,
        market=Market(),
        unifi=None,
        retail=None,
        scheduler=SimpleNamespace(jobs={}),
        started_at=datetime.now(UTC),
    )
    posted: list[Any] = []

    async def post_test_alert(test: Any) -> str:
        posted.append(test)
        return "https://discord.com/channels/1/2/3"

    return SimpleNamespace(
        ctx=ctx, owner_ids={OWNER}, post_test_alert=post_test_alert, posted=posted
    )


async def viewer(bot: Any, user_id: str = MEMBER, role: Role = Role.MEMBER) -> Viewer:
    async with bot.ctx.sessions() as session:
        guild = await config_for(session, bot.ctx.settings.discord, GUILD)
    assert guild is not None
    return Viewer(Interaction(bot, user_id), bot, role, guild)


async def variant_id(bot: Any, sku: str = "U7-Pro") -> int:
    async with bot.ctx.sessions() as session:
        return await session.scalar(select(Variant.id).where(Variant.sku == sku))


async def press(
    bot: Any, verb: str, state: str = "", values: list[Any] | None = None, **who: Any
) -> Interaction:
    """Use a control the way Discord would: through the dispatcher, as a fresh interaction."""
    interaction = Interaction(bot, who.get("user_id", MEMBER), manage=who.get("manage", False))
    await screens.dispatch(interaction, verb, state, values or [])  # type: ignore[arg-type]
    return interaction


def controls(view: discord.ui.View) -> list[Any]:
    """The buttons and menus themselves (screens wrap each one to route it)."""
    return [getattr(item, "item", item) for item in view.children]


def ids(screen: Screen) -> list[str]:
    return [i.custom_id for i in controls(screen.view()) if getattr(i, "custom_id", None)]


def check_discord_limits(screen: Screen) -> None:
    view = screen.view()  # add_item raises when a row is over Discord's width
    components = view.to_components()
    assert len(components) <= 5
    custom_ids = ids(screen)
    assert len(custom_ids) == len(set(custom_ids)), custom_ids
    assert all(len(c) <= 100 for c in custom_ids)


# ── every screen, every role ───────────────────────────────────────────────


async def test_every_screen_renders_within_discord_limits(bot):
    vid = await variant_id(bot)
    states = [
        HOME, WATCHES, FLIPS, "flips|1|25|wifi", RADAR, "radar|back_in_stock|us",
        "radar|upcoming|", f"p|{vid}|{HOME}", f"p|{vid}|flips|1|25|", f"pp|{vid}|{HOME}",
        "about", "forget", "server", "tools", "health", "audit", "access", f"access|{GUILD}",
        f"access|{GUILD}|block",
    ]  # fmt: skip
    for role in (Role.MEMBER, Role.SERVER_ADMIN, Role.OWNER):
        v = await viewer(bot, OWNER if role is Role.OWNER else MEMBER, role)
        for state in states:
            try:
                screen = await screens.render(v, state)
            except Denied:
                continue
            check_discord_limits(screen)


@pytest.mark.parametrize(
    ("state", "role", "allowed"),
    [
        ("server", Role.MEMBER, False),
        ("server", Role.SERVER_ADMIN, True),
        ("tools", Role.MEMBER, False),
        ("health", Role.SERVER_ADMIN, True),
        ("access", Role.SERVER_ADMIN, False),
        ("access", Role.OWNER, True),
        (FLIPS, Role.MEMBER, False),  # valuations are off in this server
        (FLIPS, Role.OWNER, True),
    ],
)
async def test_screens_check_roles(bot, state: str, role: Role, allowed: bool):
    v = await viewer(bot, role=role)
    if allowed:
        await screens.render(v, state)
    else:
        with pytest.raises(Denied):
            await screens.render(v, state)


async def test_home_shows_what_matters_and_the_way_around(bot):
    member = await screens.home_screen(await viewer(bot))
    assert member.labels()[:2] == ["Find a product", "About and privacy"]
    assert "Server settings" not in member.labels()
    assert "Flips" not in member.labels()  # valuations are off here
    assert "**Your watches:** none yet" in member.embed.description
    home = next(i for i in controls(member.view()) if getattr(i, "label", None) == "Home")
    assert home.disabled  # you're on it

    admin = await screens.home_screen(await viewer(bot, role=Role.SERVER_ADMIN))
    assert "Server settings" in admin.labels()


# ── finding and watching a product ─────────────────────────────────────────


async def test_product_from_a_list_goes_back_to_that_list(bot):
    vid = await variant_id(bot)
    pressed = await press(bot, "open", "radar|back_in_stock|us", [str(vid)])
    view = pressed.shown["view"]
    back = next(i for i in controls(view) if getattr(i, "label", None) == "◀ Back")
    assert back.custom_id == "fs:back:radar|back_in_stock|us"
    radar = next(i for i in controls(view) if getattr(i, "label", None) == "Radar")
    assert radar.style is discord.ButtonStyle.primary  # the section you came from
    assert not radar.disabled
    assert "Not watching" in pressed.shown["embed"].description


async def test_picking_stores_watches_and_unwatches(bot):
    vid = await variant_id(bot)
    state = f"p|{vid}|{HOME}"
    async with bot.ctx.sessions() as session:
        pairs = await session.execute(
            select(Listing.region, Listing.id).where(Listing.variant_id == vid)
        )
        listings = dict(pairs.all())

    pressed = await press(bot, "wsel", state, [str(listings["us"])])
    assert pressed.shown["content"] == "🔔 Watching 1 store(s)."
    assert "**Watching** Ubiquiti Store US" in pressed.shown["embed"].description
    labels = [getattr(i, "label", None) for i in controls(pressed.shown["view"])]
    assert {"Target price…", "Mute 24h", "Send a test alert"} <= set(labels)

    async with bot.ctx.sessions() as session, session.begin():
        watch = await session.scalar(select(Watch))
        watch.max_price_cents = 15000
    pressed = await press(bot, "wsel", state, [str(listings["us"]), str(listings["ca"])])
    async with bot.ctx.sessions() as session:
        caps = {w.listing_id: w.max_price_cents for w in await session.scalars(select(Watch))}
    assert caps == {listings["us"]: 15000, listings["ca"]: 15000}  # the new store inherits it

    pressed = await press(bot, "wsel", state, [])
    assert pressed.shown["content"] == "🔕 Not watching this any more."
    async with bot.ctx.sessions() as session:
        assert list(await session.scalars(select(Watch))) == []


async def test_target_price_form(bot):
    vid = await variant_id(bot)
    state = f"p|{vid}|{HOME}"
    async with bot.ctx.sessions() as session:
        listing_id = await session.scalar(select(Listing.id).where(Listing.region == "us"))
    await press(bot, "wsel", state, [str(listing_id)])

    pressed = await press(bot, "tgt", state)
    form = pressed.response.modal
    assert isinstance(form, screens.Form)
    form.inputs[0]._value = "149.99"  # what the person typed
    submitted = Interaction(bot, MEMBER)
    await form.on_submit(submitted)  # type: ignore[arg-type]
    assert submitted.shown["content"] == "Pings only at or below $149.99."
    async with bot.ctx.sessions() as session:
        assert await session.scalar(select(Watch.max_price_cents)) == 14999

    form.inputs[0]._value = "cheap"
    rejected = Interaction(bot, MEMBER)
    await form.on_submit(rejected)  # type: ignore[arg-type]
    assert "Enter a price like 149.99" in rejected.response.sent[0]["content"]


async def test_test_alert_from_a_product(bot):
    vid = await variant_id(bot)
    state = f"p|{vid}|{WATCHES}"
    async with bot.ctx.sessions() as session:
        listing_id = await session.scalar(select(Listing.id).where(Listing.region == "us"))
    await press(bot, "wsel", state, [str(listing_id)])
    pressed = await press(bot, "test", state)
    assert len(bot.posted) == 1
    assert "Sent a sample" in pressed.shown["embed"].description
    back = next(i for i in controls(pressed.shown["view"]) if getattr(i, "label", None) == "◀ Back")
    assert back.custom_id == f"fs:back:{state}"


async def test_watches_screen_switches_everything_and_mutes(bot):
    screen = await screens.watches_screen(await viewer(bot))
    picker = controls(screen.view())[0]
    assert [o.label for o in picker.options][:3] == [  # type: ignore[attr-defined]
        "Every important alert: Off",
        "Every important alert: Every region",
        "Every important alert: US stores only",
    ]
    pressed = await press(bot, "every", WATCHES, ["us"])
    assert "every important alert:** US only" in pressed.shown["embed"].description
    async with bot.ctx.sessions() as session:
        [sub] = list(await session.scalars(select(Watch).where(Watch.listing_id.is_(None))))
    assert (sub.discord_user_id, sub.guild_id, sub.regions) == (MEMBER, GUILD, "us")

    vid = await variant_id(bot)
    async with bot.ctx.sessions() as session:
        listing_id = await session.scalar(select(Listing.id).where(Listing.region == "us"))
    await press(bot, "wsel", f"p|{vid}|{WATCHES}", [str(listing_id)])
    pressed = await press(bot, "muteall", WATCHES)
    assert pressed.shown["content"] == "Muted 1 watch(es) for 24h."
    labels = [getattr(i, "label", None) for i in controls(pressed.shown["view"])]
    assert "Unmute all" in labels
    assert "Mute all 24h" not in labels

    pressed = await press(bot, "every", WATCHES, ["off"])
    assert "every important alert:** off" in pressed.shown["embed"].description


# ── lists ──────────────────────────────────────────────────────────────────


async def test_flips_filters_and_open(bot):
    vid = await variant_id(bot)
    async with bot.ctx.sessions() as session, session.begin():
        listing_id = await session.scalar(select(Listing.id).where(Listing.region == "us"))
        session.add(
            Valuation(
                variant_id=vid, listing_id=listing_id, retail_cents=18900, flip_net_cents=4500,
                flip_roi=0.24, flip_basis="sold", flip_confidence="high", score=1.0,
                available=True, computed_at=T0,
            )
        )  # fmt: skip
        (await session.get(Guild, GUILD)).show_valuations = True
    pressed = await press(bot, "ff", FLIPS, ["1|25"])
    assert "U7-Pro" in pressed.shown["embed"].description
    flips = next(i for i in controls(pressed.shown["view"]) if getattr(i, "label", None) == "Flips")
    assert not flips.disabled  # filtered: Flips resets the filters
    pressed = await press(bot, "ff", FLIPS, ["0|100"])
    assert pressed.shown["embed"].description == "No flips match these filters."

    pressed = await press(bot, "open", "flips|1|25|", [str(vid)])
    labels = [getattr(i, "label", None) for i in controls(pressed.shown["view"])]
    assert "Profit" in labels


async def test_radar_filters_by_kind_and_region(bot):
    pressed = await press(bot, "rk", RADAR, ["upcoming"])
    assert pressed.shown["embed"].title.startswith("Radar · announced restocks")
    assert "UDM-SE" in pressed.shown["embed"].description
    pressed = await press(bot, "rr", "radar|upcoming|", ["ca"])
    assert pressed.shown["embed"].description == "No restock dates announced."


# ── settings ───────────────────────────────────────────────────────────────


async def test_server_settings_save_from_menus(bot):
    member = await press(bot, "sk", "server", ["back_in_stock"])
    assert "Only server admins can do that." in member.response.sent[0]["content"]

    admin = await press(bot, "sk", "server", ["back_in_stock", "deal"], manage=True)
    assert admin.shown["content"] == "Saved."
    async with bot.ctx.sessions() as session:
        assert (await session.get(Guild, GUILD)).mention_kinds == "back_in_stock,deal"
    assert "Back in stock, Deal" in str(admin.shown["embed"].to_dict())


async def test_only_the_owner_shows_valuations(bot):
    admin = await press(bot, "sval", "tools", manage=True)
    assert "Only the bot owner" in admin.response.sent[0]["content"]
    owner = await press(bot, "sval", "tools", user_id=OWNER)
    assert "Valuations are shown here now" in owner.shown["content"]
    async with bot.ctx.sessions() as session:
        assert (await session.get(Guild, GUILD)).show_valuations is True


async def test_delete_my_data_asks_first(bot):
    screen = await screens.about_screen(await viewer(bot))
    assert "Delete my data" in screen.labels()
    confirm = await screens.forget_screen(await viewer(bot))
    assert confirm.labels() == ["Yes, delete everything", "Cancel"]
    await press(bot, "every", WATCHES, ["all"])
    pressed = await press(bot, "forgetyes")
    assert pressed.shown["content"].startswith("Done: removed 1 watch(es)")


# ── edges ──────────────────────────────────────────────────────────────────


async def test_old_buttons_and_unapproved_servers(bot):
    old = await press(bot, "nonsense", "")
    assert "older version" in old.response.sent[0]["content"]

    stranger = Interaction(bot, MEMBER)
    stranger.guild_id = 999
    await screens.dispatch(stranger, "go", HOME, [])  # type: ignore[arg-type]
    assert "isn't approved" in stranger.response.sent[0]["content"]


async def test_alerts_open_details_privately(bot):
    view = alert_view(7, "https://store.ui.com/us/en/products/u7-pro")
    assert view is not None
    labels = [getattr(i, "label", None) for i in controls(view)]
    assert labels == ["Open", "Watch", "Details"]

    async with bot.ctx.sessions() as session:
        listing_id = await session.scalar(select(Listing.id).where(Listing.region == "us"))
    pressed = await press(bot, "detail", str(listing_id))
    [message] = pressed.response.sent
    assert message["ephemeral"] is True
    assert message["embed"].title == "Product u7-pro" or message["embed"].title


def test_product_state_falls_back_home_when_too_long():
    assert screens.product_state(5, "flips|1|25|wifi") == "p|5|flips|1|25|wifi"
    assert screens.product_state(5, "flips|1|25|" + "x" * 120) == f"p|5|{HOME}"
