"""The bot's private, in-place navigation.

``/flipscout`` and ``/stock`` open one private message. Every button and menu in it changes that
same message (Home, Watches, Flips, Radar, a product, server settings), so nothing piles up in
the channel and there is always a way back. Each component's custom ID carries the screen it
belongs to (``fs:<verb>:<state>``), so the buttons keep working after a restart and never time
out.

States are short strings: ``home``, ``watches``, ``flips|<buyable 0/1>|<min profit $>|<category>``,
``radar|<kind>|<region>``, ``p|<variant id>|<state to go back to>`` (a product) and
``pp|<variant id>|<back>`` (its profit breakdown), plus the about and settings screens.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
from sqlalchemy import select

from tracker.alerts import AlertPlan
from tracker.audit import audit, describe, recent_audit
from tracker.bot.cleanup import cleanup_messages
from tracker.bot.embeds import KIND_STYLE, alert_content, alert_embed, source_name, status_text
from tracker.bot.render import (
    history_lines,
    opportunity_line,
    paginate,
    scan_embed,
    ts,
    value_embed,
)
from tracker.bot.views import everything_text, watch_test_embed, watches_embed
from tracker.db.models import Listing, Product, Valuation, Variant, Watch
from tracker.domain import EventKind, SourceKey, StockStatus, format_cents
from tracker.guilds import ADMIN_FIELDS, GuildConfig, GuildStatus, config_for, list_guilds
from tracker.guilds import update_config as save_config
from tracker.history import ebay_trend, stock_history
from tracker.outbox import RadarEvent, load_events
from tracker.permissions import Role, resolve_role
from tracker.queries import (
    ListingRow,
    add_watches,
    forget_user,
    opportunities,
    recent_observations,
    remove_watches,
    set_subscribed,
    set_subscription_regions,
    subscription_for,
    tracker_stats,
    upcoming_restocks,
    variant_comps,
    variant_listings,
)
from tracker.services.unifi import slug_from_url
from tracker.sources.web import parse_money
from tracker.watching import Resolved, mute_watches, parse_regions, resolve_listings
from tracker.watching import user_watch_rows as watch_rows
from tracker.watchtest import SampleAlertError, plan_watch_test

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot
    from tracker.context import TrackerContext

logger = logging.getLogger(__name__)

PREFIX = "fs"
MUTE_HOURS = 24
UPCOMING = "upcoming"  # radar kind: announced restock dates
HOME = "home"
WATCHES = "watches"
FLIPS = "flips|0|0|"
RADAR = "radar||"
BLUE = 0x5865F2
GOLD = 0xF1C40F
GREY = 0x99AAB5
_OPTIONS = 25  # Discord's limit for a select menu
_ROLE_RE = re.compile(r"<@&(\d+)>")
FLIP_FILTERS = (
    ("0|0", "All flips"),
    ("1|0", "Buyable now"),
    ("1|25", "Buyable now, $25+ profit"),
    ("0|25", "$25+ profit"),
    ("0|50", "$50+ profit"),
    ("0|100", "$100+ profit"),
)
CONDITIONS = {"new": "new", "open box": "open_box", "open_box": "open_box", "used": "used"}


class Denied(Exception):
    """Shown to the person as-is (a permission or input problem, not a bug)."""


# ── who is looking ─────────────────────────────────────────────────────────


async def access(
    bot: TrackerBot, interaction: discord.Interaction[Any]
) -> tuple[Role, GuildConfig | None]:
    """The person's role here and this server's settings (None outside an approved server)."""
    config: GuildConfig | None = None
    if interaction.guild_id is not None:
        async with bot.ctx.sessions() as session:
            config = await config_for(session, bot.ctx.settings.discord, interaction.guild_id)
    permissions = getattr(interaction.user, "guild_permissions", None)
    role = resolve_role(
        interaction.user.id,
        owner_ids=bot.owner_ids,
        global_admin_ids=bot.ctx.settings.discord.admin_user_ids,
        manage_guild=bool(permissions and permissions.manage_guild),
        role_ids=[r.id for r in getattr(interaction.user, "roles", [])],
        guild=config,
    )
    return role, config


@dataclass
class Viewer:
    """The person using a screen, and the interaction to answer."""

    interaction: discord.Interaction[Any]
    bot: TrackerBot
    role: Role
    guild: GuildConfig
    verb: str = ""  # which kind of button or menu was used

    @property
    def ctx(self) -> TrackerContext:
        return self.bot.ctx

    @property
    def user_id(self) -> str:
        return str(self.interaction.user.id)

    @property
    def guild_id(self) -> str:
        return self.guild.guild_id

    @property
    def admin(self) -> bool:
        return self.role >= Role.SERVER_ADMIN

    @property
    def owner(self) -> bool:
        return self.role >= Role.OWNER

    @property
    def valuations(self) -> bool:
        return self.owner or self.guild.show_valuations

    def need(self, role: Role) -> None:
        if self.role < role:
            who = "the bot owner" if role is Role.OWNER else "server admins"
            raise Denied(f"Only {who} can do that.")

    def need_valuations(self) -> None:
        if not self.valuations:
            raise Denied("Resale valuations aren't enabled in this server.")

    async def show(self, screen: Screen, content: str | None = None) -> None:
        """Replace this message with a screen."""
        kwargs: dict[str, Any] = {
            "content": content if content is not None else screen.content,
            "embed": screen.embed,
            "view": screen.view(),
        }
        if self.interaction.response.is_done():
            await self.interaction.edit_original_response(**kwargs)
        else:
            await self.interaction.response.edit_message(**kwargs)

    async def open(self, screen: Screen) -> None:
        """Send a screen as a new private message (from a slash command or a public alert)."""
        kwargs: dict[str, Any] = {
            "embed": screen.embed,
            "view": screen.view(),
            "ephemeral": True,
        }
        if screen.content:
            kwargs["content"] = screen.content
        if self.interaction.response.is_done():
            await self.interaction.followup.send(**kwargs)
        else:
            await self.interaction.response.send_message(**kwargs)

    async def say(self, text: str) -> None:
        if self.interaction.response.is_done():
            await self.interaction.followup.send(text, ephemeral=True)
        else:
            await self.interaction.response.send_message(text, ephemeral=True)

    async def defer(self) -> None:
        """Acknowledge a slow change; the screen is replaced when it's done."""
        if not self.interaction.response.is_done():
            await self.interaction.response.defer()

    async def audit(self, action: str, target: str | None = None, **details: Any) -> None:
        async with self.ctx.sessions() as session, session.begin():
            await audit(
                session,
                action,
                actor_id=self.interaction.user.id,
                actor_name=str(self.interaction.user),
                guild_id=self.interaction.guild_id,
                target=target,
                details=details or None,
            )


async def viewer_for(interaction: discord.Interaction[Any]) -> Viewer | None:
    """The viewer behind a button press, or None (after saying why) outside approved servers."""
    bot: TrackerBot = interaction.client  # type: ignore[assignment]
    role, config = await access(bot, interaction)
    if config is None or not config.approved:
        await interaction.response.send_message(
            "This server isn't approved to use the tracker.", ephemeral=True
        )
        return None
    return Viewer(interaction, bot, role, config)


# ── components ─────────────────────────────────────────────────────────────


def cid(verb: str, state: str = "") -> str:
    custom_id = f"{PREFIX}:{verb}:{state}"
    if len(custom_id) > 100:
        raise ValueError(f"custom ID too long: {custom_id}")
    return custom_id


class Nav(
    discord.ui.DynamicItem[discord.ui.Item[Any]],
    template=r"fs:(?P<verb>[a-z]+):(?P<state>.*)",
):
    """Any button or menu on a screen. The custom ID says what it does and where it is."""

    def __init__(self, item: discord.ui.Item[Any], *, row: int | None = None) -> None:
        super().__init__(item, row=row)

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Nav:
        return cls(item)

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        _, verb, state = str(self.custom_id).split(":", 2)
        await dispatch(interaction, verb, state, list(getattr(self.item, "values", []) or []))


def button(
    label: str,
    verb: str,
    state: str = "",
    *,
    row: int,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> Nav:
    return Nav(
        discord.ui.Button(label=label, style=style, custom_id=cid(verb, state), disabled=disabled),
        row=row,
    )


def menu(
    placeholder: str,
    verb: str,
    state: str,
    options: list[discord.SelectOption],
    *,
    row: int,
    min_values: int = 1,
    max_values: int = 1,
) -> Nav:
    return Nav(
        discord.ui.Select(
            custom_id=cid(verb, state),
            placeholder=placeholder[:150],
            options=options[:_OPTIONS],
            min_values=min_values,
            max_values=min(max_values, len(options[:_OPTIONS])),
        ),
        row=row,
    )


def option(label: str, value: str, description: str | None = None, *, default: bool = False):
    return discord.SelectOption(
        label=label[:100] or "?",
        value=value[:100],
        description=(description or "")[:100] or None,
        default=default,
    )


@dataclass
class Screen:
    embed: discord.Embed
    items: list[discord.ui.Item[Any]]
    content: str | None = None

    def view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for item in self.items:
            view.add_item(item)
        return view

    def labels(self) -> list[str]:
        """Every button label and menu placeholder (for tests and docs)."""
        found = []
        for item in self.items:
            inner = getattr(item, "item", item)
            found.append(getattr(inner, "label", None) or getattr(inner, "placeholder", ""))
        return found


TABS = (("Home", HOME), ("Watches", WATCHES), ("Flips", FLIPS), ("Radar", RADAR))


def parts(state: str, count: int, maxsplit: int = -1) -> list[str]:
    """A state's '|'-separated fields, padded with empty strings."""
    fields = state.split("|", maxsplit)
    return [*fields, *[""] * count][:count]


def tab_of(state: str) -> str:
    return state.split("|", 1)[0]


def nav_row(
    v: Viewer, section: str | None, back: str | None = None, *, current: str | None = None
) -> list[discord.ui.Item[Any]]:
    """The bottom row: Back (when there is somewhere to go back to) and the four sections.
    The section you're in is highlighted; it's disabled when you're on exactly its screen."""
    items: list[discord.ui.Item[Any]] = []
    if back:
        items.append(button("◀ Back", "back", back, row=4))
    for label, state in TABS:
        if state == FLIPS and not v.valuations:
            continue
        inside = section is not None and tab_of(state) == section
        items.append(
            button(
                label,
                "go",
                state,
                row=4,
                style=discord.ButtonStyle.primary if inside else discord.ButtonStyle.secondary,
                disabled=state == current,
            )
        )
    return items


def product_state(variant_id: int, back: str) -> str:
    """A product screen remembering where it was opened from (Home if that's too long)."""
    state = f"p|{variant_id}|{back}"
    longest = f"{PREFIX}:unmute:{state}"  # the longest verb used on a product screen
    return state if len(longest) <= 100 else f"p|{variant_id}|{HOME}"


def split_product(state: str) -> tuple[int, str]:
    _, variant, back = parts(state, 3, maxsplit=2)
    if not variant.isdigit():
        raise Denied("That product link is broken. Open it again.")
    return int(variant), back or HOME


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _muted(watch: Watch, now: datetime) -> bool:
    return watch.muted_until is not None and _aware(watch.muted_until) > now


def _place(listing: Listing) -> str:
    return f"{source_name(listing.source)} {listing.region.upper()}"


def _status_emoji(status: str) -> str:
    return status_text(StockStatus(status)).split(" ")[0]


# ── shared lookups ─────────────────────────────────────────────────────────


async def resolve(ctx: TrackerContext, text: str, region: str | None) -> Resolved:
    """A SKU, product name, or store URL → listings (fetching unknown URLs on demand)."""
    return await resolve_listings(
        ctx.sessions,
        text,
        region,
        poll_unifi=ctx.unifi.poll_product,
        add_retail_url=ctx.retail.add_url if ctx.retail is not None else None,
    )


async def stock_embed(ctx: TrackerContext, rows: list[ListingRow]) -> discord.Embed:
    first = rows[0]
    embed = discord.Embed(title=first.product.name[:256], url=first.listing.url, color=BLUE)
    now = datetime.now(UTC)
    async with ctx.sessions() as session:
        histories = {r.listing.id: await stock_history(session, r.listing.id) for r in rows[:20]}
        history = await recent_observations(session, first.listing.id, limit=6)
    for row in rows[:20]:
        listing = row.listing
        lines = [
            status_text(StockStatus(listing.status))
            + f" · {format_cents(listing.price_cents, listing.currency)}"
        ]
        lines.extend(history_lines(listing, histories[listing.id], now))
        if listing.restock_eta and listing.status != StockStatus.IN_STOCK:
            lines.append(f"Restock ETA {listing.restock_eta.isoformat()}")
        if listing.is_early_access:
            lines.append("⚡ Early Access")
        lines.append(
            f"In stock {ts(listing.last_in_stock_at)} · checked {ts(listing.last_checked_at)}"
        )
        embed.add_field(
            name=f"{row.variant.sku} · {_place(listing)}"[:256],
            value="\n".join(lines),
            inline=False,
        )
    if history:
        embed.add_field(
            name=f"Recent changes ({first.variant.sku} {first.listing.region.upper()})",
            value="\n".join(
                f"{ts(o.observed_at, 'd')} {status_text(StockStatus(o.status))} · "
                f"{format_cents(o.price_cents, first.listing.currency)}"
                for o in history
            ),
            inline=False,
        )
    embed.set_footer(text=f"{first.product.category or 'uncategorized'} · {first.product.brand}")
    return embed


async def profit_embed(ctx: TrackerContext, variant_id: int, *, refresh: bool) -> discord.Embed:
    live = None
    if refresh and ctx.market.collecting:
        live = (await ctx.market.collect(variant_id)).items
    async with ctx.sessions() as session:
        variant = await session.get(Variant, variant_id)
        valuation = await session.get(Valuation, variant_id)
        comps = await variant_comps(session, variant_id, limit=8)
        product = await session.get(Product, variant.product_id) if variant else None
        trend = await ebay_trend(session, variant_id)
    if variant is None:
        raise Denied("That product is gone.")
    return value_embed(
        variant, product, valuation, comps, ctx.settings.market, live=live, trend=trend
    )


async def check_now(ctx: TrackerContext, rows: list[ListingRow]) -> None:
    """Poll these listings right away and deliver any alerts."""
    pages = {
        (r.listing.region, slug)
        for r in rows
        if r.listing.source == SourceKey.UNIFI and (slug := slug_from_url(r.listing.url))
    }
    for region, slug in pages:
        await ctx.unifi.poll_product(region, slug)
    for row in rows:
        if row.listing.source != SourceKey.UNIFI and ctx.retail is not None:
            await ctx.retail.poll(row.listing.id, row.listing.url, SourceKey(row.listing.source))
    await ctx.dispatcher.dispatch()


def event_line(event: RadarEvent) -> str:
    emoji, label, _ = KIND_STYLE.get(event.kind, ("•", event.kind.value, 0))
    name = f"[{event.sku}]({event.url})" if event.url else (event.sku or "?")
    where = f" {event.region.upper()}" if event.region else ""
    change = (
        f" · {event.old_value or '—'} → {event.new_value or '—'}"
        if event.kind in (EventKind.PRICE_CHANGE, EventKind.RESTOCK_ETA)
        else ""
    )
    return f"{ts(event.occurred_at)} {emoji} {label} · {name}{where}{change}"


async def _variant_ids(ctx: TrackerContext, listing_ids: set[int]) -> dict[int, int]:
    if not listing_ids:
        return {}
    async with ctx.sessions() as session:
        pairs = await session.execute(
            select(Listing.id, Listing.variant_id).where(Listing.id.in_(listing_ids))
        )
    return dict(pairs.all())


def open_menu(
    placeholder: str, back: str, products: list[tuple[int, str, str | None]], *, row: int
) -> list[discord.ui.Item[Any]]:
    """'Open …' menu: (variant id, label, description), first mention of each product wins."""
    seen: set[int] = set()
    options = []
    for variant_id, label, description in products:
        if variant_id in seen:
            continue
        seen.add(variant_id)
        options.append(option(label, str(variant_id), description))
    return [menu(placeholder, "open", back, options, row=row)] if options else []


# ── member screens ─────────────────────────────────────────────────────────


async def home_screen(v: Viewer, state: str = HOME) -> Screen:
    now = datetime.now(UTC)
    async with v.ctx.sessions() as session:
        rows = await watch_rows(session, v.user_id)
        subscription = await subscription_for(session, v.user_id, v.guild_id)
        events = await load_events(session, pending_only=False, limit=5)
    buyable = [r for r in rows if StockStatus(r.listing.status).is_available]
    if rows:
        lines = [f"**Your watches:** {len(rows)} · {len(buyable)} in stock now"]
        lines += [
            f"{_status_emoji(r.listing.status)} **{r.variant.sku}** {_place(r.listing)} · "
            f"{format_cents(r.listing.price_cents, r.listing.currency)}"
            for r in buyable[:5]
        ]
    else:
        lines = ["**Your watches:** none yet. Find a product, then pick the stores to watch."]
    lines.append(f"**Ping me for every important alert:** {everything_text(subscription)}")
    if events:
        lines += ["", "**Latest**", *(event_line(e) for e in events)]
    embed = discord.Embed(title="FlipScout", description="\n".join(lines)[:4000], color=BLUE)
    embed.set_footer(text=f"Only you can see this · {now:%H:%M} UTC")
    items: list[discord.ui.Item[Any]] = [
        button("Find a product", "find", HOME, row=0, style=discord.ButtonStyle.primary),
        button("About and privacy", "go", "about", row=0),
    ]
    if v.admin:
        items.append(button("Server settings", "go", "server", row=0))
    items += open_menu(
        "Open a watch that's in stock…",
        HOME,
        [(r.variant.id, f"{r.variant.sku} · {_place(r.listing)}", r.product.name) for r in buyable],
        row=1,
    )
    return Screen(embed, items + nav_row(v, HOME, current=HOME))


def everything_menu(subscription: Watch | None, regions: list[str]) -> Nav:
    current = "off"
    if subscription is not None:
        chosen = parse_regions(subscription.regions)
        current = "all" if chosen is None else ",".join(sorted(chosen))
    choices = [("off", "Off"), ("all", "Every region")]
    choices += [(r, f"{r.upper()} stores only") for r in regions]
    return menu(
        f"Ping me for every important alert: {everything_text(subscription)}",
        "every",
        WATCHES,
        [
            option(f"Every important alert: {label}", value, default=value == current)
            for value, label in choices
        ],
        row=0,
    )


async def watches_screen(v: Viewer, state: str = WATCHES) -> Screen:
    now = datetime.now(UTC)
    async with v.ctx.sessions() as session:
        rows = await watch_rows(session, v.user_id)
        subscription = await subscription_for(session, v.user_id, v.guild_id)
    embed = watches_embed(rows, now, subscription)
    items: list[discord.ui.Item[Any]] = []
    regions = list(v.ctx.settings.unifi.regions)
    if regions:
        items.append(everything_menu(subscription, regions))
    items += open_menu(
        "Open a watch to change it…",
        WATCHES,
        [
            (
                r.variant.id,
                f"{r.variant.sku} · {_place(r.listing)}",
                f"{status_text(StockStatus(r.listing.status))} · {r.product.name}",
            )
            for r in rows
        ],
        row=1,
    )
    if any(not _muted(r.watch, now) for r in rows):
        items.append(button(f"Mute all {MUTE_HOURS}h", "muteall", WATCHES, row=2))
    if any(_muted(r.watch, now) for r in rows):
        items.append(button("Unmute all", "unmuteall", WATCHES, row=2))
    return Screen(embed, items + nav_row(v, WATCHES, current=WATCHES))


def split_flips(state: str) -> tuple[bool, int, str]:
    _, buyable, minimum, category = parts(state, 4)
    return buyable == "1", int(minimum) if minimum.isdigit() else 0, category


def flips_state(buyable: bool, minimum: int, category: str) -> str:
    return f"flips|{int(buyable)}|{minimum}|{category}"


async def flips_screen(v: Viewer, state: str = FLIPS) -> Screen:
    v.need_valuations()
    buyable, minimum, category = split_flips(state)
    async with v.ctx.sessions() as session:
        rows = await opportunities(
            session,
            limit=15,
            min_profit_cents=minimum * 100 or None,
            available_only=buyable,
            category=category or None,
        )
        categories = [
            c
            for c in await session.scalars(
                select(Product.category)
                .join(Variant, Variant.product_id == Product.id)
                .join(Valuation, Valuation.variant_id == Variant.id)
                .where(Valuation.flip_net_cents.is_not(None), Product.category.is_not(None))
                .distinct()
                .order_by(Product.category)
            )
            if c
        ]
    settings = v.ctx.settings.market
    if rows:
        description = paginate([opportunity_line(i, r) for i, r in enumerate(rows, start=1)])
    elif buyable or minimum or category:
        description = "No flips match these filters."
    elif v.ctx.market.collecting:
        description = "No flips yet. Prices are still being collected from eBay."
    else:
        description = "No flips yet. eBay isn't set up (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)."
    embed = discord.Embed(title="Flips", description=description, color=GOLD)
    embed.set_footer(
        text=f"Net after {settings.fee_rate:.1%} + {format_cents(settings.fixed_fee_cents)} "
        f"fees, {format_cents(settings.outbound_shipping_cents)} shipping, "
        f"{settings.purchase_tax_rate:.1%} tax · open one for the breakdown"
    )
    items = open_menu(
        "Open a flip…",
        state,
        [
            (r.variant.id, f"{i}. {r.variant.sku}", f"{format_cents(r.valuation.flip_net_cents)} · "
             f"{r.product.name}")
            for i, r in enumerate(rows, start=1)
        ],
        row=0,
    )  # fmt: skip
    current = f"{int(buyable)}|{minimum}"
    filters = [option(label, value, default=value == current) for value, label in FLIP_FILTERS]
    if not any(o.default for o in filters):
        filters.insert(0, option(f"Custom: {minimum}+", current, default=True))
    items.append(menu("Show…", "ff", state, filters, row=1))
    if categories:
        items.append(
            menu(
                "Category…",
                "fc",
                state,
                [option("Every category", "*", default=not category)]
                + [option(c, c, default=c == category) for c in categories[: _OPTIONS - 1]],
                row=2,
            )
        )
    return Screen(embed, items + nav_row(v, "flips", current=state))


def split_radar(state: str) -> tuple[str, str]:
    _, kind, region = parts(state, 3)
    return kind, region


async def radar_screen(v: Viewer, state: str = RADAR) -> Screen:
    kind, region = split_radar(state)
    products: list[tuple[int, str, str | None]] = []
    if kind == UPCOMING:
        async with v.ctx.sessions() as session:
            rows = await upcoming_restocks(session, datetime.now(UTC).date(), 30)
        rows = [r for r in rows if not region or r.listing.region == region]
        lines = [
            f"`{r.listing.restock_eta}` **{r.variant.sku}** {r.listing.region.upper()} · "
            f"{format_cents(r.listing.price_cents, r.listing.currency)}"
            for r in rows
        ]
        title = f"Radar · announced restocks, next 30 days ({len(rows)})"
        empty = "No restock dates announced."
        products = [
            (r.variant.id, f"{r.variant.sku} · {r.listing.region.upper()}", r.product.name)
            for r in rows
        ]
    else:
        async with v.ctx.sessions() as session:
            events = await load_events(
                session,
                pending_only=False,
                limit=200 if region else 15,
                kinds=[kind] if kind else None,
            )
        events = [e for e in events if not region or (e.region or "") == region][:15]
        lines = [event_line(e) for e in events]
        label = KIND_STYLE[EventKind(kind)][1] if kind in EventKind else "Everything"
        title = f"Radar · {label}"
        empty = "Nothing on the radar yet."
        variants = await _variant_ids(v.ctx, {e.listing_id for e in events if e.listing_id})
        products = [
            (variants[e.listing_id], f"{e.sku or '?'} · {(e.region or '').upper()}", e.product_name)
            for e in events
            if e.listing_id in variants
        ]
    if region:
        title += f" · {region.upper()}"
    embed = discord.Embed(title=title, description=paginate(lines) or empty, color=BLUE)
    items = open_menu("Open a product…", state, products, row=0)
    kinds = [option("Everything", "*", default=not kind)]
    kinds += [
        option(label, k.value, default=k.value == kind) for k, (_, label, _) in KIND_STYLE.items()
    ]
    kinds.append(option("Upcoming restocks", UPCOMING, "Announced dates", default=kind == UPCOMING))
    items.append(menu("What to show…", "rk", state, kinds, row=1))
    regions = list(v.ctx.settings.unifi.regions)
    if regions:
        items.append(
            menu(
                "Region…",
                "rr",
                state,
                [option("Every region", "*", default=not region)]
                + [option(r.upper(), r, default=r == region) for r in regions],
                row=2,
            )
        )
    return Screen(embed, items + nav_row(v, "radar", current=state))


async def _my_watches(v: Viewer, listing_ids: list[int]) -> dict[int, Watch]:
    async with v.ctx.sessions() as session:
        return {
            w.listing_id: w
            for w in await session.scalars(
                select(Watch).where(
                    Watch.discord_user_id == v.user_id, Watch.listing_id.in_(listing_ids)
                )
            )
            if w.listing_id is not None
        }


def watch_summary(rows: list[ListingRow], watches: dict[int, Watch], now: datetime) -> str:
    watched = [r for r in rows if r.listing.id in watches]
    if not watched:
        return (
            "**Not watching.** Pick stores in the first menu to get pinged when they restock, "
            "go Early Access, or drop in price."
        )
    parts = [f"**Watching** {', '.join(_place(r.listing) for r in watched)}"]
    caps = {watches[r.listing.id].max_price_cents for r in watched}
    if caps != {None}:
        cap = next(c for c in caps if c is not None)
        parts.append(f"pings at or below {format_cents(cap, watched[0].listing.currency)}")
    muted = [watches[r.listing.id] for r in watched if _muted(watches[r.listing.id], now)]
    if muted:
        until = max(_aware(w.muted_until) for w in muted if w.muted_until)
        parts.append(f"🔕 muted until <t:{int(until.timestamp())}:R>")
    return " · ".join(parts)


async def product_screen(v: Viewer, state: str) -> Screen:
    variant_id, back = split_product(state)
    async with v.ctx.sessions() as session:
        rows = await variant_listings(session, variant_id)
    if not rows:
        embed = discord.Embed(title="That product is gone", color=GREY)
        return Screen(embed, nav_row(v, tab_of(back), back))
    now = datetime.now(UTC)
    watches = await _my_watches(v, [r.listing.id for r in rows])
    embed = await stock_embed(v.ctx, rows)
    embed.description = watch_summary(rows, watches, now)
    shown = rows[:_OPTIONS]
    items: list[discord.ui.Item[Any]] = [
        menu(
            f"Watching {len(watches)} store(s): change…" if watches else "Watch: pick stores…",
            "wsel",
            state,
            [
                option(
                    f"{_place(r.listing)} · {status_text(StockStatus(r.listing.status))}",
                    str(r.listing.id),
                    format_cents(r.listing.price_cents, r.listing.currency),
                    default=r.listing.id in watches,
                )
                for r in shown
            ],
            row=0,
            min_values=0,
            max_values=len(shown),
        )
    ]
    link = next(
        (
            r.listing.url
            for r in rows
            if r.listing.url and StockStatus(r.listing.status).is_available
        ),
        next((r.listing.url for r in rows if r.listing.url), None),
    )
    if link:
        items.append(discord.ui.Button(label="Open at store ↗", url=link, row=1))
    if v.valuations:
        items.append(button("Profit", "go", f"pp|{variant_id}|{back}", row=1))
    if v.admin:
        items.append(button("Check now", "chk", state, row=1))
    if watches:
        items.append(button("Target price…", "tgt", state, row=2))
        if any(_muted(w, now) for w in watches.values()):
            items.append(button("Unmute", "unmute", state, row=2))
        else:
            items.append(button(f"Mute {MUTE_HOURS}h", "mute", state, row=2))
        items.append(button("Send a test alert", "test", state, row=2))
    if v.owner:
        items.append(button("Record a sale…", "sale", state, row=3))
    return Screen(embed, items + nav_row(v, tab_of(back), back))


async def profit_screen(v: Viewer, state: str, *, refresh: bool = False) -> Screen:
    v.need_valuations()
    variant_id, back = split_product(state)
    embed = await profit_embed(v.ctx, variant_id, refresh=refresh)
    items: list[discord.ui.Item[Any]] = []
    if v.owner and v.ctx.market.collecting:
        items.append(button("Refresh from eBay", "ebay", state, row=0))
    if v.owner:
        items.append(button("Record a sale…", "sale", state, row=0))
    return Screen(embed, items + nav_row(v, tab_of(back), f"p|{variant_id}|{back}"))


async def about_screen(v: Viewer, state: str = "about") -> Screen:
    embed = discord.Embed(
        title="About FlipScout",
        color=BLUE,
        description=(
            "A private stock and price tracker run by this server's owner.\n\n"
            "**Using it:** `/flipscout` opens your home screen and `/stock` jumps straight to a "
            "product. On a product, pick the stores to watch; you're pinged when they restock, "
            "go Early Access, or drop in price. Everything you open is private to you.\n\n"
            "**What it stores about you:** your Discord user ID, the products you watch (with "
            "any target price), and your alert settings. It can't read messages, and only the "
            "bot's operator sees it."
        ),
    )
    items = [button("Delete my data", "go", "forget", row=0, style=discord.ButtonStyle.danger)]
    return Screen(embed, items + nav_row(v, None))


async def forget_screen(v: Viewer, state: str = "forget") -> Screen:
    embed = discord.Embed(
        title="Delete your data?",
        description="All your watches and alert settings, in every server. This can't be undone.",
        color=0xED4245,
    )
    items = [
        button("Yes, delete everything", "forgetyes", "", row=0, style=discord.ButtonStyle.danger),
        button("Cancel", "go", "about", row=0),
    ]
    return Screen(embed, items)


# ── server screens (admins) ────────────────────────────────────────────────


def _channel(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "—"


def _kind_label(kind: str) -> str:
    style = KIND_STYLE.get(EventKind(kind)) if kind in EventKind else None
    return style[1] if style else kind


async def server_screen(v: Viewer, state: str = "server") -> Screen:
    v.need(Role.SERVER_ADMIN)
    g = v.guild
    embed = discord.Embed(title=f"{g.name or 'This server'} · alerts", color=BLUE)
    embed.description = "Changes save as soon as you pick them."
    embed.add_field(name="Alerts channel", value=_channel(g.alert_channel_id))
    embed.add_field(
        name="Radar channel",
        value=_channel(g.radar_channel_id) + ("" if g.radar_channel_id else " (uses alerts)"),
    )
    embed.add_field(name="Pings", value=g.alert_mention or "—")
    embed.add_field(
        name="Pings for",
        value=", ".join(sorted(_kind_label(k) for k in g.mention_kinds)) or "—",
        inline=False,
    )
    embed.add_field(name="Repeat cooldown", value=f"{g.cooldown_minutes:g} min")
    embed.add_field(name="Digest after", value=f"{g.digest_threshold} events")
    text = [discord.ChannelType.text, discord.ChannelType.news]
    mention = _ROLE_RE.fullmatch(g.alert_mention or "")
    items: list[discord.ui.Item[Any]] = [
        Nav(
            discord.ui.ChannelSelect(
                custom_id=cid("sch", "server"),
                channel_types=text,
                placeholder="Alerts channel",
                default_values=[discord.Object(g.alert_channel_id)] if g.alert_channel_id else [],
            ),
            row=0,
        ),
        Nav(
            discord.ui.ChannelSelect(
                custom_id=cid("srd", "server"),
                channel_types=text,
                placeholder="Radar channel (empty: the alerts channel)",
                min_values=0,
                default_values=[discord.Object(g.radar_channel_id)] if g.radar_channel_id else [],
            ),
            row=1,
        ),
        Nav(
            discord.ui.RoleSelect(
                custom_id=cid("srl", "server"),
                placeholder="Role to ping on important alerts (empty: none)",
                min_values=0,
                default_values=[discord.Object(int(mention[1]))] if mention else [],
            ),
            row=2,
        ),
        menu(
            "Ping for…",
            "sk",
            "server",
            [
                option(label, k.value, default=k.value in g.mention_kinds)
                for k, (_, label, _) in KIND_STYLE.items()
            ],
            row=3,
            min_values=0,
            max_values=len(KIND_STYLE),
        ),
        button("◀ Home", "back", HOME, row=4),
        button("Timing…", "stime", "server", row=4),
        button("More settings and tools", "go", "tools", row=4),
    ]
    return Screen(embed, items)


async def tools_screen(v: Viewer, state: str = "tools") -> Screen:
    v.need(Role.SERVER_ADMIN)
    g = v.guild
    embed = discord.Embed(title=f"{g.name or 'This server'} · tools", color=BLUE)
    embed.add_field(
        name="Admin role",
        value=f"<@&{g.admin_role_id}>" if g.admin_role_id else "Manage Server only",
    )
    embed.add_field(name="Resale valuations", value="shown" if g.show_valuations else "hidden")
    items: list[discord.ui.Item[Any]] = [
        Nav(
            discord.ui.RoleSelect(
                custom_id=cid("sar", "tools"),
                placeholder="Admin role (empty: Manage Server only)",
                min_values=0,
                default_values=[discord.Object(g.admin_role_id)] if g.admin_role_id else [],
            ),
            row=0,
        ),
        button("Send a test alert here", "stest", "tools", row=1),
        button("Delete old bot messages", "sclean", "tools", row=1),
        button("Health", "go", "health", row=2),
        button("Audit log", "go", "audit", row=2),
    ]
    if v.owner:
        label = "Hide valuations here" if g.show_valuations else "Show valuations here"
        items.append(button(label, "sval", "tools", row=3))
        items.append(button("Servers and access", "go", "access", row=3))
        if v.ctx.market.collecting:
            items.append(button("Price the store against eBay…", "sscan", "tools", row=3))
    items += [button("◀ Back", "back", "server", row=4), button("Home", "go", HOME, row=4)]
    return Screen(embed, items)


async def health_screen(v: Viewer, state: str = "health") -> Screen:
    v.need(Role.SERVER_ADMIN)
    now = datetime.now(UTC)
    async with v.ctx.sessions() as session:
        stats = await tracker_stats(session, now)
    embed = discord.Embed(title="Health", color=0x57F287, timestamp=now)
    embed.add_field(name="Uptime", value=str(now - v.ctx.started_at).split(".")[0])
    embed.add_field(name="Watches", value=f"{stats.watches} · {stats.subscribers} subscribers")
    embed.add_field(
        name="Events", value=f"{stats.events_24h} in 24h · {stats.pending_events} pending"
    )
    for region in sorted({region for region, _ in stats.status_counts}):
        counts = {s: n for (r, s), n in stats.status_counts.items() if r == region}
        lines = [f"{status_text(StockStatus(s))}: {n}" for s, n in sorted(counts.items())]
        lines.append(f"Last check {ts(stats.last_checked.get(region))}")
        embed.add_field(name=f"{region.upper()} ({sum(counts.values())})", value="\n".join(lines))
    mono = time.monotonic()  # the scheduler's clock
    jobs = []
    for job in v.ctx.scheduler.jobs.values():
        when = "running" if job.running else f"next in {max(0, int(job.next_run - mono))}s"
        health = f" · ⚠️ {job.failures} failures" if job.failures else ""
        jobs.append(f"`{job.name}` {job.runs} runs · {when}{health}")
    embed.add_field(name="Jobs", value="\n".join(jobs)[:1024] or "—", inline=False)
    items = [
        button("◀ Back", "back", "tools", row=4),
        button("Refresh", "go", "health", row=4),
        button("Home", "go", HOME, row=4),
    ]
    return Screen(embed, items)


async def audit_screen(v: Viewer, state: str = "audit") -> Screen:
    v.need(Role.SERVER_ADMIN)
    scope = None if v.owner else v.guild_id
    async with v.ctx.sessions() as session:
        entries = await recent_audit(session, limit=20, guild_id=scope)
    text = "\n".join(f"`{describe(e)}`"[:300] for e in entries) or "No entries."
    embed = discord.Embed(title="Audit log", description=text[:4000], color=GREY)
    if v.owner:
        embed.set_footer(text="Every server (you're the owner)")
    items = [button("◀ Back", "back", "tools", row=4), button("Home", "go", HOME, row=4)]
    return Screen(embed, items)


STATUS_ICON = {GuildStatus.APPROVED: "✅", GuildStatus.PENDING: "⏳", GuildStatus.BLOCKED: "⛔"}


async def access_screen(v: Viewer, state: str = "access") -> Screen:
    v.need(Role.OWNER)
    _, chosen, step = parts(state, 3)
    async with v.ctx.sessions() as session:
        rows = await list_guilds(session)
    lines = [
        f"{STATUS_ICON.get(GuildStatus(r.status), '?')} "
        f"**{discord.utils.escape_markdown(r.name or '?')}** `{r.guild_id}`"
        f"{' · valuations' if r.show_valuations else ''}"
        for r in rows
    ]
    embed = discord.Embed(title="Servers", description=paginate(lines, 40) or "None.", color=BLUE)
    items: list[discord.ui.Item[Any]] = []
    if rows:
        items.append(
            menu(
                "Choose a server…",
                "go",
                "access-pick",
                [
                    option(
                        f"{STATUS_ICON.get(GuildStatus(r.status), '')} {r.name or r.guild_id}",
                        f"access|{r.guild_id}",
                        r.guild_id,
                        default=r.guild_id == chosen,
                    )
                    for r in rows
                ],
                row=0,
            )
        )
    picked = next((r for r in rows if r.guild_id == chosen), None)
    content = None
    if picked is not None and step == "block":
        content = (
            f"Block **{discord.utils.escape_markdown(picked.name or picked.guild_id)}**? "
            "The bot leaves it and its members lose access."
        )
        items += [
            button(
                "Yes, block", "ablock", picked.guild_id, row=1, style=discord.ButtonStyle.danger
            ),
            button("Cancel", "go", f"access|{picked.guild_id}", row=1),
        ]
    elif picked is not None:
        items += [
            button(
                "Approve",
                "aok",
                picked.guild_id,
                row=1,
                style=discord.ButtonStyle.success,
                disabled=picked.status == GuildStatus.APPROVED,
            ),
            button(
                "Block…",
                "go",
                f"access|{picked.guild_id}|block",
                row=1,
                style=discord.ButtonStyle.danger,
                disabled=picked.status == GuildStatus.BLOCKED,
            ),
        ]
    items += [button("◀ Back", "back", "tools", row=4), button("Home", "go", HOME, row=4)]
    return Screen(embed, items, content)


SCREENS: dict[str, Callable[[Viewer, str], Awaitable[Screen]]] = {
    "home": home_screen,
    "watches": watches_screen,
    "flips": flips_screen,
    "radar": radar_screen,
    "p": product_screen,
    "pp": profit_screen,
    "about": about_screen,
    "forget": forget_screen,
    "server": server_screen,
    "tools": tools_screen,
    "health": health_screen,
    "audit": audit_screen,
    "access": access_screen,
}


async def render(v: Viewer, state: str) -> Screen:
    builder = SCREENS.get(tab_of(state), home_screen)
    return await builder(v, state)


# ── forms ──────────────────────────────────────────────────────────────────

Submit = Callable[[Viewer, list[str]], Awaitable[None]]


class Form(discord.ui.Modal):
    """A pop-up form; on submit, the same screen is redrawn with the change."""

    def __init__(
        self, title: str, viewer: Viewer, fields: list[discord.ui.TextInput[Any]], submit: Submit
    ) -> None:
        super().__init__(title=title, timeout=900)
        self.viewer = viewer
        self.inputs = fields
        self.submit = submit
        for field in fields:
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction[Any]) -> None:
        viewer = replace(self.viewer, interaction=interaction)
        try:
            await self.submit(viewer, [str(f.value or "").strip() for f in self.inputs])
        except Denied as exc:
            await viewer.say(f"⚠️ {exc}")

    async def on_error(
        self, interaction: discord.Interaction[Any], error: Exception, /
    ) -> None:  # pragma: no cover - logged for the operator
        logger.exception("Form %s failed", self.title, exc_info=error)
        await replace(self.viewer, interaction=interaction).say(
            "⚠️ Something went wrong. The error was logged."
        )


def field(
    label: str,
    *,
    default: str = "",
    placeholder: str = "",
    required: bool = False,
    max_length: int = 100,
) -> discord.ui.TextInput[Any]:
    return discord.ui.TextInput(
        label=label,
        default=default or None,
        placeholder=placeholder or None,
        required=required,
        max_length=max_length,
    )


def _number(text: str, low: float, high: float, what: str) -> float:
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        raise Denied(f"{what} must be a number.") from None
    if not low <= value <= high:
        raise Denied(f"{what} must be between {low:g} and {high:g}.")
    return value


# ── actions ────────────────────────────────────────────────────────────────

Handler = Callable[[Viewer, str, list[Any]], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def handles(*verbs: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        for verb in verbs:
            HANDLERS[verb] = handler
        return handler

    return register


async def dispatch(
    interaction: discord.Interaction[Any], verb: str, state: str, values: list[Any]
) -> None:
    handler = HANDLERS.get(verb)
    if handler is None:
        await interaction.response.send_message(
            "That button is from an older version. Run /flipscout again.", ephemeral=True
        )
        return
    viewer = await viewer_for(interaction)
    if viewer is None:
        return
    viewer.verb = verb
    try:
        await handler(viewer, state, values)
    except Denied as exc:
        await viewer.say(f"⚠️ {exc}")
    except Exception:
        logger.exception("Screen action %s (%s) failed", verb, state)
        await viewer.say("⚠️ Something went wrong. The error was logged.")


@handles("go", "back")
async def go(v: Viewer, state: str, values: list[Any]) -> None:
    await v.show(await render(v, str(values[0]) if values else state))


@handles("open")
async def open_product(v: Viewer, back: str, values: list[Any]) -> None:
    await v.show(await product_screen(v, product_state(int(values[0]), back)))


@handles("detail")
async def detail(v: Viewer, listing_id: str, values: list[Any]) -> None:
    """From a public alert: open the product privately (a new message, not an edit)."""
    variants = await _variant_ids(v.ctx, {int(listing_id)} if listing_id.isdigit() else set())
    if not variants:
        raise Denied("That listing is gone.")
    await v.open(await product_screen(v, product_state(next(iter(variants.values())), HOME)))


@handles("ff")
async def flip_filter(v: Viewer, state: str, values: list[Any]) -> None:
    _, _, category = split_flips(state)
    buyable, _, minimum = str(values[0]).partition("|")
    await v.show(await flips_screen(v, flips_state(buyable == "1", int(minimum or 0), category)))


@handles("fc")
async def flip_category(v: Viewer, state: str, values: list[Any]) -> None:
    buyable, minimum, _ = split_flips(state)
    category = "" if values[0] == "*" else str(values[0])
    await v.show(await flips_screen(v, flips_state(buyable, minimum, category)))


@handles("rk", "rr")
async def radar_filter(v: Viewer, state: str, values: list[Any]) -> None:
    kind, region = split_radar(state)
    chosen = "" if values[0] == "*" else str(values[0])
    if v.verb == "rk":
        kind = chosen
    else:
        region = chosen
    await v.show(await radar_screen(v, f"radar|{kind}|{region}"))


@handles("every")
async def everything(v: Viewer, state: str, values: list[Any]) -> None:
    choice = str(values[0])
    async with v.ctx.sessions() as session, session.begin():
        await set_subscribed(session, v.user_id, choice != "off", v.guild_id)
        if choice != "off":
            await session.flush()
            regions = None if choice == "all" else choice
            await set_subscription_regions(session, v.user_id, regions, v.guild_id)
    await v.show(await watches_screen(v))


@handles("muteall", "unmuteall")
async def mute_all(v: Viewer, state: str, values: list[Any]) -> None:
    muting = v.verb == "muteall"
    until = datetime.now(UTC) + timedelta(hours=MUTE_HOURS) if muting else None
    async with v.ctx.sessions() as session, session.begin():
        ids = [r.watch.id for r in await watch_rows(session, v.user_id)]
        count = await mute_watches(session, v.user_id, ids, until)
    done = (
        f"Muted {count} watch(es) for {MUTE_HOURS}h." if muting else f"Unmuted {count} watch(es)."
    )
    await v.show(await watches_screen(v), content=done)


async def _product_rows(v: Viewer, state: str) -> list[ListingRow]:
    variant_id, _ = split_product(state)
    async with v.ctx.sessions() as session:
        rows = await variant_listings(session, variant_id)
    if not rows:
        raise Denied("That product is gone.")
    return rows


@handles("wsel")
async def watch_stores(v: Viewer, state: str, values: list[Any]) -> None:
    rows = await _product_rows(v, state)
    shown = {r.listing.id for r in rows[:_OPTIONS]}
    chosen = {int(x) for x in values} & shown
    current = await _my_watches(v, list(shown))
    caps = [w.max_price_cents for w in current.values() if w.max_price_cents]
    async with v.ctx.sessions() as session, session.begin():
        if chosen - set(current):
            new = sorted(chosen - set(current))
            await add_watches(session, v.user_id, new, caps[0] if caps else None, v.guild_id)
        if set(current) - chosen:
            await remove_watches(session, v.user_id, sorted(set(current) - chosen))
    if chosen == set(current):
        note = None
    elif chosen:
        note = f"🔔 Watching {len(chosen)} store(s)."
    else:
        note = "🔕 Not watching this any more."
    await v.show(await product_screen(v, state), content=note)


@handles("chk")
async def check(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    rows = await _product_rows(v, state)
    await v.defer()
    await v.audit("stock.check", rows[0].variant.sku)
    await check_now(v.ctx, rows)
    await v.show(await product_screen(v, state), content="Checked just now.")


@handles("mute", "unmute")
async def mute_product(v: Viewer, state: str, values: list[Any]) -> None:
    rows = await _product_rows(v, state)
    muting = v.verb == "mute"
    watches = await _my_watches(v, [r.listing.id for r in rows])
    until = datetime.now(UTC) + timedelta(hours=MUTE_HOURS) if muting else None
    async with v.ctx.sessions() as session, session.begin():
        await mute_watches(session, v.user_id, [w.id for w in watches.values()], until)
    await v.show(await product_screen(v, state))


@handles("tgt")
async def target(v: Viewer, state: str, values: list[Any]) -> None:
    rows = await _product_rows(v, state)
    watches = await _my_watches(v, [r.listing.id for r in rows])
    if not watches:
        raise Denied("Watch at least one store first.")
    caps = [w.max_price_cents for w in watches.values() if w.max_price_cents]
    current = f"{caps[0] / 100:.2f}" if caps else ""

    async def submit(viewer: Viewer, answers: list[str]) -> None:
        cap = parse_money(answers[0]) if answers[0] else None
        if answers[0] and cap is None:
            raise Denied("Enter a price like 149.99, or leave it empty.")
        async with viewer.ctx.sessions() as session, session.begin():
            await add_watches(session, viewer.user_id, sorted(watches), cap, viewer.guild_id)
        note = f"Pings only at or below {format_cents(cap)}." if cap else "Pings at any price."
        await viewer.show(await product_screen(viewer, state), content=note)

    await v.interaction.response.send_modal(
        Form(
            "Target price",
            v,
            [
                field(
                    "Ping only at or below (empty: any price)",
                    default=current,
                    placeholder="149.99",
                )
            ],
            submit,
        )
    )


@handles("test")
async def test_alert(v: Viewer, state: str, values: list[Any]) -> None:
    rows = await _product_rows(v, state)
    watches = await _my_watches(v, [r.listing.id for r in rows])
    if not watches:
        raise Denied("Watch at least one store first.")
    first = next(watches[r.listing.id] for r in rows if r.listing.id in watches)
    await v.defer()
    async with v.ctx.sessions() as session:
        planned = await plan_watch_test(session, v.ctx.settings, first.id, user_id=v.user_id)
    if planned is None:
        raise Denied("That watch is gone.")
    link: str | None = None
    error: str | None = None
    try:
        link = await v.bot.post_test_alert(planned)
    except SampleAlertError as exc:
        error = str(exc)
    await v.audit(
        "watch.test",
        planned.sku,
        alert=planned.selected.key,
        sent=bool(link),
        error=error,
    )
    _, back = split_product(state)
    await v.show(Screen(watch_test_embed(planned, link, error), nav_row(v, tab_of(back), state)))


@handles("sale")
async def record_sale(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.OWNER)
    variant_id, _ = split_product(state)

    async def submit(viewer: Viewer, answers: list[str]) -> None:
        price_text, shipping_text, quantity_text, condition_text, extra = answers
        price = parse_money(price_text)
        if price is None:
            raise Denied("Enter the sale price, like 249.99.")
        shipping = parse_money(shipping_text) if shipping_text else None
        quantity = int(_number(quantity_text or "1", 1, 50, "Quantity"))
        condition = CONDITIONS.get((condition_text or "new").lower())
        if condition is None:
            raise Denied("Condition is new, open box, or used.")
        url = extra if extra.startswith(("http://", "https://")) else None
        note = None if url else (extra or None)
        async with viewer.ctx.sessions() as session:
            variant = await session.get(Variant, variant_id)
        if variant is None:
            raise Denied("That product is gone.")
        await viewer.audit("market.comp", variant.sku, price=price / 100, quantity=quantity)
        result = await viewer.ctx.market.add_manual_comp(
            variant_id,
            price,
            shipping_cents=shipping,
            condition=condition,
            quantity=quantity,
            url=url,
            note=note,
            created_by=viewer.user_id,
        )
        summary = (
            f"net {format_cents(result.net_cents)} ({result.roi:.0%} ROI)"
            if result and result.net_cents is not None and result.roi is not None
            else f"needs {viewer.ctx.settings.market.min_comps} sales for a valuation"
        )
        await viewer.show(
            await render(viewer, state), content=f"✅ Recorded {format_cents(price)}: {summary}."
        )

    await v.interaction.response.send_modal(
        Form(
            "Record a sale",
            v,
            [
                field("Sale price", placeholder="249.99", required=True),
                field("Shipping the buyer paid", placeholder="0"),
                field("Units in the sale", default="1"),
                field("Condition (new, open box, used)", default="new"),
                field("Link or note", max_length=300),
            ],
            submit,
        )
    )


@handles("ebay")
async def refresh_ebay(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.OWNER)
    await v.defer()
    await v.show(await profit_screen(v, state, refresh=True), content="Refreshed from eBay.")


@handles("find")
async def find(v: Viewer, back: str, values: list[Any]) -> None:
    async def submit(viewer: Viewer, answers: list[str]) -> None:
        await viewer.defer()
        found = await resolve(viewer.ctx, answers[0], None)
        if found.error or not found.rows:
            raise Denied(found.error or "Nothing found.")
        await viewer.show(
            await product_screen(viewer, product_state(found.rows[0].variant.id, back))
        )

    await v.interaction.response.send_modal(
        Form(
            "Find a product",
            v,
            [
                field(
                    "SKU, name, or store link",
                    placeholder="U7-Pro, Dream Machine SE, or a store URL",
                    required=True,
                    max_length=300,
                )
            ],
            submit,
        )
    )


@handles("forgetyes")
async def forget(v: Viewer, state: str, values: list[Any]) -> None:
    async with v.ctx.sessions() as session, session.begin():
        counts = await forget_user(session, v.user_id)
        # Logged without the member's identity: the point is to forget them.
        await audit(session, "member.forget", guild_id=v.interaction.guild_id, details=counts)
    await v.show(
        await about_screen(v), content=f"Done: removed {counts['watches']} watch(es) and settings."
    )


# ── server actions ─────────────────────────────────────────────────────────


async def _configure(v: Viewer, changes: dict[str, Any], back: str, note: str = "Saved.") -> None:
    allowed = ADMIN_FIELDS | ({"show_valuations"} if v.owner else frozenset())
    async with v.ctx.sessions() as session, session.begin():
        await save_config(
            session,
            v.guild_id,
            changes,
            allowed=frozenset(allowed),
            actor_id=v.user_id,
            actor_name=str(v.interaction.user),
        )
        fresh = await config_for(session, v.ctx.settings.discord, v.guild_id)
    viewer = replace(v, guild=fresh or v.guild)
    await viewer.show(await render(viewer, back), content=note)


def _can_post(channel: Any) -> bool:
    """Whether the bot can post embeds in a picked channel (unknown counts as yes)."""
    resolved = channel.resolve() if hasattr(channel, "resolve") else None
    if resolved is None:
        return True
    perms = resolved.permissions_for(resolved.guild.me) if resolved.guild.me else None
    return bool(perms and perms.send_messages and perms.embed_links)


@handles("sch", "srd")
async def set_channel(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    key = "alert_channel_id" if v.verb == "sch" else "radar_channel_id"
    channel = values[0] if values else None
    note = "Saved."
    if channel is not None and not _can_post(channel):
        note = f"Saved, but I can't post embeds in {channel.mention}; fix its permissions."
    await _configure(v, {key: str(channel.id) if channel else None}, state, note)


@handles("srl")
async def set_mention(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    await _configure(v, {"alert_mention": values[0].mention if values else None}, state)


@handles("sar")
async def set_admin_role(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    await _configure(v, {"admin_role_id": str(values[0].id) if values else None}, state)


@handles("sk")
async def set_kinds(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    kinds = [str(k) for k in values if str(k) in EventKind]
    await _configure(v, {"mention_kinds": ",".join(kinds)}, state)


@handles("stime")
async def timing(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    g = v.guild

    async def submit(viewer: Viewer, answers: list[str]) -> None:
        cooldown = _number(answers[0], 0, 1440, "Cooldown")
        digest = int(_number(answers[1], 1, 100, "Digest threshold"))
        await _configure(viewer, {"cooldown_minutes": cooldown, "digest_threshold": digest}, state)

    await v.interaction.response.send_modal(
        Form(
            "Alert timing",
            v,
            [
                field("Minutes before repeating the same alert", default=f"{g.cooldown_minutes:g}"),
                field(
                    "Combine routine events into a digest after", default=str(g.digest_threshold)
                ),
            ],
            submit,
        )
    )


@handles("stest")
async def server_test(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    channel = v.interaction.channel
    if channel is None:
        raise Denied("Run this from a channel.")
    await v.audit("alert.test", EventKind.BACK_IN_STOCK.value)
    event = RadarEvent(
        id=0,
        kind=EventKind.BACK_IN_STOCK,
        occurred_at=datetime.now(UTC),
        old_value=None,
        new_value=None,
        listing_id=None,
        source="unifi",
        region="us",
        url="https://store.ui.com/us/en/products/u7-pro",
        sku="U7-Pro",
        product_name="Access Point U7 Pro (TEST)",
        category="wifi-flagship",
        pack_size=1,
        status=StockStatus.IN_STOCK,
        price_cents=18900,
        currency="USD",
        restock_eta=None,
        is_early_access=False,
    )
    plan = AlertPlan(event=event, important=True, channel_id=None, mention_user_ids=[v.user_id])
    from tracker.bot.views import alert_view

    await channel.send(  # type: ignore[union-attr]
        content=alert_content(plan), embed=alert_embed(event), view=alert_view(None, event.url)
    )
    await v.show(await tools_screen(v), content="Posted a sample alert in this channel.")


@handles("sclean")
async def server_cleanup(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.SERVER_ADMIN)
    await v.defer()
    g = v.guild
    channels = [c for c in (g.alert_channel_id, g.radar_channel_id) if c]
    deleted = await cleanup_messages(v.bot, channels, v.ctx.settings.cleanup, datetime.now(UTC))
    await v.audit("messages.cleanup", deleted=deleted)
    await v.show(await tools_screen(v), content=f"Deleted {deleted} old bot message(s).")


@handles("sval")
async def toggle_valuations(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.OWNER)
    show = not v.guild.show_valuations
    note = (
        "Valuations are shown here now. eBay's license treats showing them to other people as "
        "displaying eBay content; keep it to servers that are just you."
        if show
        else "Valuations are hidden here now."
    )
    await _configure(v, {"show_valuations": show}, state, note)


@handles("sscan")
async def scan(v: Viewer, state: str, values: list[Any]) -> None:
    v.need(Role.OWNER)
    if v.ctx.market.scanning:
        raise Denied("A scan is already running; try again in a minute.")

    async def submit(viewer: Viewer, answers: list[str]) -> None:
        limit = int(_number(answers[0], 5, 60, "Items"))
        minimum = _number(answers[1], 0, 10000, "Minimum retail")
        await viewer.defer()
        report = await viewer.ctx.market.scan(limit=limit, min_retail_cents=round(minimum * 100))
        await viewer.audit("market.scan", f"{report.priced} items", calls=report.calls)
        embed = scan_embed(report, viewer.ctx.settings.market)
        await viewer.show(
            Screen(
                embed, [button("◀ Back", "back", "tools", row=4), button("Home", "go", HOME, row=4)]
            )
        )

    await v.interaction.response.send_modal(
        Form(
            "Price the store against eBay",
            v,
            [
                field("How many items (one eBay call each, 5-60)", default="25"),
                field("Only items costing at least ($)", default="100"),
            ],
            submit,
        )
    )


@handles("aok", "ablock")
async def decide_server(v: Viewer, guild_id: str, values: list[Any]) -> None:
    v.need(Role.OWNER)
    if not guild_id.isdigit():
        raise Denied("Server IDs are numbers.")
    status = GuildStatus.APPROVED if v.verb == "aok" else GuildStatus.BLOCKED
    await v.defer()
    await v.bot.decide_guild(int(guild_id), status, v.interaction.user)
    await v.show(
        await access_screen(v, f"access|{guild_id}"),
        content=f"Server `{guild_id}` → {status.value}.",
    )
