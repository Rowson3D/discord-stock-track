"""Test a watch: what an alert for it looks like, where it goes, and whether it pings you.

A test builds a sample event for the watch's real product and runs it through the same
routing as real alerts (`AlertRouter`), looking only at this watch. Rules, "ping me for
everything" subscriptions, and server-wide role mentions are left out, so the answer is
about this watch alone. Nothing is written to the database and no SMS is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.alerts import AlertPlan, AlertRouter, strip_valuation
from tracker.db.models import Listing, Product, Valuation, Variant, Watch
from tracker.domain import EventKind, StockStatus, format_cents
from tracker.guilds import GuildConfig, config_for, home_guild_id
from tracker.outbox import RadarEvent
from tracker.settings import Settings
from tracker.watching import ALERT_KEYS, parse_kinds

SAMPLE_KINDS: dict[str, EventKind] = {
    "back_in_stock": EventKind.BACK_IN_STOCK,
    "early_access": EventKind.EARLY_ACCESS,
    "price_drop": EventKind.PRICE_CHANGE,
    "deal": EventKind.DEAL,
    "new_listing": EventKind.NEW_LISTING,
    "restock_eta": EventKind.RESTOCK_ETA,
    "sold_out": EventKind.SOLD_OUT,
}
_SOLD_OUT_KEYS = frozenset({"sold_out", "restock_eta"})


class SampleAlertError(Exception):
    """A test alert couldn't be posted (the message says why, for the user)."""


class AlertPoster(Protocol):
    """Posts test alerts (the Discord bot)."""

    async def post_test_alert(self, test: WatchTest) -> str: ...
    def channel_label(self, channel_id: int) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    """What would happen for one alert type on this watch."""

    key: str
    label: str
    pings: bool
    posted: bool  # Would a message be posted at all (pinging or not)?
    channel_id: int | None
    important: bool
    reason: str
    sms: bool = False


@dataclass(slots=True)
class WatchTest:
    watch_id: int
    user_id: str
    sku: str
    name: str | None
    region: str
    guild: GuildConfig | None
    selected: Verdict
    event: RadarEvent
    plan: AlertPlan | None
    coverage: list[Verdict] = field(default_factory=list)

    @property
    def mention_user_ids(self) -> list[str]:
        """Only the tester is mentioned; a test never pings roles or other people."""
        return [self.user_id] if self.selected.pings else []


def default_key(watch: Watch) -> str:
    chosen = parse_kinds(watch.kinds)
    if chosen:
        return next(k for k in ALERT_KEYS if k in chosen)
    return "back_in_stock"


def sample_event(
    key: str,
    listing: Listing,
    variant: Variant,
    product: Product,
    valuation: Valuation | None,
    now: datetime,
) -> RadarEvent:
    """A realistic event of this alert type for the real listing (id 0: never stored)."""
    kind = SAMPLE_KINDS[key]
    currency = listing.currency or "USD"
    price = listing.price_cents
    old_value = new_value = None
    restock_eta = listing.restock_eta
    if key == "price_drop":
        new_price = price or 10000
        price = new_price
        old_value = format_cents(round(new_price * 1.1), currency)
        new_value = format_cents(new_price, currency)
    elif key == "restock_eta":
        restock_eta = (now + timedelta(days=14)).date()
        old_value = listing.restock_eta.isoformat() if listing.restock_eta else None
        new_value = restock_eta.isoformat()
    status = StockStatus.OUT_OF_STOCK if key in _SOLD_OUT_KEYS else StockStatus.IN_STOCK
    valued = valuation is not None and valuation.flip_net_cents is not None
    scarce = valued and valuation is not None and valuation.flip_basis == "scarcity"
    if key == "deal" and valued and valuation is not None:
        new_value = f"net {format_cents(valuation.flip_net_cents)} · ROI {valuation.flip_roi:.0%}"
    return RadarEvent(
        id=0,
        kind=kind,
        occurred_at=now,
        old_value=old_value,
        new_value=new_value,
        listing_id=listing.id,
        source=listing.source,
        region=listing.region,
        url=listing.url,
        sku=variant.sku,
        product_name=product.name,
        category=product.category,
        pack_size=variant.pack_size,
        status=status,
        price_cents=price,
        currency=currency,
        restock_eta=restock_eta,
        is_early_access=key == "early_access" or bool(listing.is_early_access),
        resale_cents=(
            (valuation.scarcity_resale_cents if scarce else valuation.resale_cents)
            if valued and valuation is not None
            else None
        ),
        net_cents=valuation.flip_net_cents if valued and valuation is not None else None,
        roi=valuation.flip_roi if valued and valuation is not None else None,
        valuation_basis=valuation.flip_basis if valued and valuation is not None else None,
        valuation_confidence=(
            valuation.flip_confidence if valued and valuation is not None else None
        ),
        comps_count=(
            ((valuation.scarcity_days if scarce else valuation.sold_count) or 0)
            if valued and valuation is not None
            else 0
        ),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _why_not(watch: Watch, key: str, event: RadarEvent, now: datetime) -> str:
    label = ALERT_KEYS[key]
    if watch.muted_until is not None and _aware(watch.muted_until) > now:
        return f"This watch is muted until {_aware(watch.muted_until):%b %d %H:%M} UTC."
    if watch.max_price_cents is not None and (
        event.price_cents is None or event.price_cents > watch.max_price_cents
    ):
        return (
            f"You only get pinged at or below {format_cents(watch.max_price_cents, event.currency)}"
            f"; it's {format_cents(event.price_cents, event.currency)} now."
        )
    chosen = parse_kinds(watch.kinds)
    if chosen is not None and not chosen:
        return "Every alert type is turned off for this watch."
    if chosen is not None:
        return f"You turned off {label} for this watch."
    return (
        f"This watch uses the server's defaults, which don't ping for {label}. "
        "Pick alert types for this watch to change that."
    )


def _why(watch: Watch, key: str) -> str:
    label = ALERT_KEYS[key]
    chosen = parse_kinds(watch.kinds)
    if chosen is not None:
        return f"You picked {label} for this watch."
    if key == "price_drop":
        return "Price drops on anything you watch always ping you."
    return f"The server pings watchers for {label} by default."


def verdict(
    watch: Watch,
    key: str,
    event: RadarEvent,
    guild: GuildConfig | None,
    now: datetime,
    *,
    sms_kinds: frozenset[str] = frozenset(),
) -> tuple[Verdict, AlertPlan | None]:
    label = ALERT_KEYS[key]
    sms = event.kind.value in sms_kinds
    if guild is None:
        reason = "No Discord server is set up for alerts yet."
        return Verdict(key, label, False, False, None, False, reason, sms), None
    if not guild.approved:
        reason = "The watch's Discord server isn't approved, so nothing is posted there."
        return Verdict(key, label, False, False, None, False, reason, sms), None
    shown = event if guild.show_valuations else strip_valuation(event)
    plan = AlertRouter._plan_one(shown, [watch], [], [], guild, now)
    pings = watch.discord_user_id in plan.mention_user_ids
    if key == "deal" and not guild.show_valuations:
        reason = "This server hides valuations, so deals are never posted there."
        return Verdict(key, label, False, False, None, False, reason, sms), plan
    if plan.channel_id is None:
        reason = "The server has no alert channel set (use /server configure in Discord)."
        return Verdict(key, label, False, False, None, plan.important, reason, sms), plan
    reason = _why(watch, key) if pings else _why_not(watch, key, event, now)
    if not pings:
        if plan.important or plan.channel_id == guild.alert_channel_id:
            where = "alert channel"
        else:
            where = "radar channel"
        if not plan.important:
            where += " (possibly in a digest)"
        reason += f" It still posts in the {where}, without a ping."
    return Verdict(key, label, pings, True, plan.channel_id, plan.important, reason, sms), plan


async def plan_watch_test(
    session: AsyncSession,
    settings: Settings,
    watch_id: int,
    *,
    user_id: str | None = None,
    key: str | None = None,
    now: datetime | None = None,
) -> WatchTest | None:
    """Plan a test for a watch (None if it doesn't exist or isn't the user's)."""
    now = now or datetime.now(UTC)
    row = (
        await session.execute(
            select(Watch, Listing, Variant, Product, Valuation)
            .join(Listing, Watch.listing_id == Listing.id)
            .join(Variant, Listing.variant_id == Variant.id)
            .join(Product, Variant.product_id == Product.id)
            .outerjoin(Valuation, Valuation.variant_id == Variant.id)
            .where(Watch.id == watch_id)
        )
    ).first()
    if row is None:
        return None
    watch, listing, variant, product, valuation = row
    if user_id is not None and watch.discord_user_id != user_id:
        return None
    if key is not None and key not in ALERT_KEYS:
        raise ValueError(f"unknown alert type: {key}")

    home = await home_guild_id(session, settings.discord)
    guild_id = watch.guild_id or home
    guild = await config_for(session, settings.discord, guild_id) if guild_id else None
    if guild is None and settings.discord.channel_id:
        # Single-server install whose server isn't recorded yet (same as the Discord sink).
        guild = GuildConfig.from_settings(settings.discord, guild_id=guild_id or "global")
    sms_kinds = frozenset(settings.sms.kinds) if settings.sms.configured else frozenset()

    coverage: list[Verdict] = []
    chosen_key = key or default_key(watch)
    selected: tuple[Verdict, AlertPlan | None, RadarEvent] | None = None
    for each in ALERT_KEYS:
        event = sample_event(each, listing, variant, product, valuation, now)
        result, plan = verdict(watch, each, event, guild, now, sms_kinds=sms_kinds)
        coverage.append(result)
        if each == chosen_key:
            selected = (result, plan, event)
    assert selected is not None
    chosen, plan, event = selected
    return WatchTest(
        watch_id=watch.id,
        user_id=watch.discord_user_id,
        sku=variant.sku,
        name=product.name,
        region=listing.region,
        guild=guild,
        selected=chosen,
        event=event if guild is None or guild.show_valuations else strip_valuation(event),
        plan=plan,
        coverage=coverage,
    )
