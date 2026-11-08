"""Panel routes for managing what's tracked: watches, rules, priorities, and subscription.

Watches and rules made in the panel belong to the owner's Discord account (the first
`DISCORD_OWNER_IDS` entry) in the home server, so pings land where the owner already is.
The owner can also see and remove members' watches (moderation), but not edit them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from tracker.db.models import Listing, Product, Valuation, Variant, Watch, WatchRule
from tracker.domain import Priority
from tracker.guilds import home_guild_id
from tracker.queries import add_watches, set_subscribed
from tracker.watching import ALERT_KEYS, RULE_DEFAULT_KINDS, Resolved, format_kinds, parse_kinds

Record = Callable[..., Awaitable[None]]
Resolver = Callable[[str, str | None], Awaitable[Resolved]]
MAX_MUTE_HOURS = 24 * 90


class AddWatch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    region: str | None = Field(default=None, max_length=8)
    max_price_cents: int | None = Field(default=None, ge=1)
    kinds: list[str] | None = None


class EditWatch(BaseModel):
    max_price_cents: int | None = Field(default=None, ge=1)
    kinds: list[str] | None = None
    mute_hours: float | None = Field(default=None, ge=0, le=MAX_MUTE_HOURS)  # 0 = unmute


class BulkWatches(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["remove", "mute", "unmute"]
    hours: float = Field(default=24, gt=0, le=MAX_MUTE_HOURS)


class PriorityBody(BaseModel):
    priority: Literal["high", "normal", "low"]


class SubscriptionBody(BaseModel):
    subscribed: bool


class RuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    query: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    regions: list[str] | None = None
    kinds: list[str] | None = None
    max_price_cents: int | None = Field(default=None, ge=1)
    min_profit_cents: int | None = Field(default=None, ge=0)
    enabled: bool = True


class EditRule(RuleBody):
    name: str | None = Field(default=None, min_length=1, max_length=80)  # type: ignore[assignment]
    enabled: bool | None = None  # type: ignore[assignment]
    mute_hours: float | None = Field(default=None, ge=0, le=MAX_MUTE_HOURS)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _kinds_or_422(kinds: list[str] | None) -> str | None:
    try:
        return format_kinds(kinds)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _regions(values: list[str] | None) -> str | None:
    if not values:
        return None
    cleaned = sorted({v.strip().lower() for v in values if v.strip()})
    if any(len(v) > 8 or not v.isalnum() for v in cleaned):
        raise HTTPException(status_code=422, detail="regions are short codes like us, ca, uk")
    return ",".join(cleaned) or None


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def _rule_json(rule: WatchRule, owner_id: str | None) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "query": rule.query,
        "category": rule.category,
        "regions": rule.regions.split(",") if rule.regions else [],
        "kinds": sorted(parse_kinds(rule.kinds) or RULE_DEFAULT_KINDS),
        "custom_kinds": rule.kinds is not None,
        "max_price_cents": rule.max_price_cents,
        "min_profit_cents": rule.min_profit_cents,
        "enabled": rule.enabled,
        "muted_until": _iso(rule.muted_until),
        "user_id": rule.discord_user_id,
        "owner": rule.discord_user_id == owner_id,
        "created_at": _iso(rule.created_at),
    }


def _label(slug: str) -> str:
    words = slug.replace("_", "-").split("-")
    return " ".join(words).capitalize()


def category_groups(counts: dict[str, int]) -> list[dict[str, Any]]:
    """Families of categories sharing a 1- or 2-word prefix (e.g. physical-security-*)."""
    coverage: dict[str, frozenset[str]] = {}
    for slug in counts:
        parts = slug.split("-")
        for size in (1, 2):
            if len(parts) > size:
                prefix = "-".join(parts[:size])
                coverage[prefix] = coverage.get(prefix, frozenset()) | {slug}
    best: dict[frozenset[str], str] = {}
    for prefix, members in coverage.items():
        if len(members) < 2:
            continue
        if members not in best or len(prefix) > len(best[members]):
            best[members] = prefix  # identical coverage: keep the more specific name
    return sorted(
        (
            {
                "value": prefix,
                "label": f"{_label(prefix)} (all {len(members)})",
                "count": sum(counts[m] for m in members),
            }
            for members, prefix in best.items()
        ),
        key=lambda group: group["value"],
    )


def add_watchlist_routes(
    app: FastAPI,
    *,
    sessions: Any,
    settings: Any,
    owner: Callable[..., str],
    record: Record,
    owner_id: Callable[[], str | None],
    resolve: Resolver | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    def require_owner_id() -> str:
        user = owner_id()
        if not user:
            raise HTTPException(
                status_code=409,
                detail="Set DISCORD_OWNER_IDS so panel watches can ping your Discord account.",
            )
        return user

    async def home() -> str | None:
        async with sessions() as session:
            return await home_guild_id(session, settings.discord)

    def mute_until(hours: float) -> datetime | None:
        return clock() + timedelta(hours=hours) if hours > 0 else None

    @app.get("/api/watchlist")
    async def watchlist(_: str = Depends(owner)) -> dict[str, Any]:
        me = owner_id()
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(Watch, Listing, Variant, Product, Valuation)
                    .join(Listing, Watch.listing_id == Listing.id)
                    .join(Variant, Listing.variant_id == Variant.id)
                    .join(Product, Variant.product_id == Product.id)
                    .outerjoin(Valuation, Valuation.variant_id == Variant.id)
                    .order_by(Variant.sku, Listing.region, Watch.id)
                )
            ).all()
            rules = list(await session.scalars(select(WatchRule).order_by(WatchRule.id)))
            subscriptions = list(
                await session.scalars(select(Watch).where(Watch.listing_id.is_(None)))
            )
            home_id = await home_guild_id(session, settings.discord)
        items = []
        for watch, listing, variant, product, valuation in rows:
            kinds = parse_kinds(watch.kinds)
            items.append(
                {
                    "watch_id": watch.id,
                    "user_id": watch.discord_user_id,
                    "owner": watch.discord_user_id == me,
                    "guild_id": watch.guild_id or home_id,
                    "max_price_cents": watch.max_price_cents,
                    "kinds": sorted(kinds) if kinds is not None else None,
                    "muted_until": _iso(watch.muted_until),
                    "created_at": _iso(watch.created_at),
                    "listing": {
                        "id": listing.id,
                        "variant_id": variant.id,
                        "sku": variant.sku,
                        "name": product.name,
                        "category": product.category,
                        "pack_size": variant.pack_size,
                        "source": listing.source,
                        "region": listing.region,
                        "status": listing.status,
                        "delisted": listing.delisted_at is not None,
                        "price_cents": listing.price_cents,
                        "currency": listing.currency,
                        "url": listing.url,
                        "priority": listing.priority,
                        "early_access": listing.is_early_access,
                    },
                    "flip_net_cents": valuation.flip_net_cents if valuation else None,
                }
            )
        return {
            "owner_configured": bool(me),
            "subscribed": any(
                s.discord_user_id == me and (s.guild_id or home_id) == home_id
                for s in subscriptions
            ),
            "subscribers": len({s.discord_user_id for s in subscriptions}),
            "items": items,
            "rules": [_rule_json(r, me) for r in rules],
            "alert_keys": ALERT_KEYS,
            "rule_default_kinds": sorted(RULE_DEFAULT_KINDS),
            "regions": list(settings.unifi.regions),
        }

    @app.post("/api/watchlist")
    async def add(body: AddWatch, client: str = Depends(owner)) -> dict[str, Any]:
        user = require_owner_id()
        kinds = _kinds_or_422(body.kinds)
        region = (body.region or "").strip().lower() or None
        if resolve is not None:
            resolved = await resolve(body.query, region)
        else:  # Tests / headless: catalog search only.
            from tracker.watching import resolve_listings

            resolved = await resolve_listings(sessions, body.query, region)
        if resolved.error or not resolved.rows:
            raise HTTPException(status_code=404, detail=resolved.error or "Nothing found.")
        ids = [row.listing.id for row in resolved.rows]
        guild = await home()
        async with sessions() as session, session.begin():
            created = await add_watches(session, user, ids, body.max_price_cents, guild)
            if body.kinds is not None:
                for watch in await session.scalars(
                    select(Watch).where(Watch.discord_user_id == user, Watch.listing_id.in_(ids))
                ):
                    watch.kinds = kinds
        await record(
            "watch.add",
            client,
            target=resolved.rows[0].variant.sku,
            details={
                "listings": len(ids),
                "created": created,
                "max_price_cents": body.max_price_cents,
            },
        )
        return {"listings": len(ids), "created": created}

    async def _watch(session: Any, watch_id: int) -> Watch:
        watch = await session.get(Watch, watch_id)
        if watch is None or watch.listing_id is None:
            raise HTTPException(status_code=404, detail="no such watch")
        return watch

    @app.patch("/api/watches/{watch_id}")
    async def edit(watch_id: int, body: EditWatch, client: str = Depends(owner)) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        async with sessions() as session, session.begin():
            watch = await _watch(session, watch_id)
            if watch.discord_user_id != owner_id():
                raise HTTPException(
                    status_code=403, detail="you can remove members' watches, not edit them"
                )
            fields = body.model_fields_set
            if "max_price_cents" in fields:
                watch.max_price_cents = changes["max_price_cents"] = body.max_price_cents
            if "kinds" in fields:
                watch.kinds = _kinds_or_422(body.kinds)
                changes["kinds"] = watch.kinds
            if "mute_hours" in fields:
                watch.muted_until = mute_until(body.mute_hours or 0)
                changes["muted_until"] = _iso(watch.muted_until)
        await record("watch.update", client, target=str(watch_id), details=changes)
        return {"watch_id": watch_id, **changes}

    @app.delete("/api/watches/{watch_id}")
    async def remove(watch_id: int, client: str = Depends(owner)) -> dict[str, Any]:
        async with sessions() as session, session.begin():
            watch = await _watch(session, watch_id)
            user = watch.discord_user_id
            await session.delete(watch)
        await record("watch.remove", client, target=str(watch_id), details={"user_id": user})
        return {"removed": 1}

    @app.post("/api/watches/bulk")
    async def bulk(body: BulkWatches, client: str = Depends(owner)) -> dict[str, Any]:
        ids = sorted(set(body.ids))
        async with sessions() as session, session.begin():
            if body.action == "remove":
                result = await session.execute(
                    delete(Watch).where(Watch.id.in_(ids), Watch.listing_id.is_not(None))
                )
                count = result.rowcount or 0
            else:
                until = mute_until(body.hours) if body.action == "mute" else None
                count = 0
                for watch in await session.scalars(
                    select(Watch).where(Watch.id.in_(ids), Watch.discord_user_id == owner_id())
                ):
                    watch.muted_until = until
                    count += 1
        await record(f"watch.bulk_{body.action}", client, details={"ids": ids, "count": count})
        return {"action": body.action, "count": count}

    @app.post("/api/listings/{listing_id}/priority")
    async def priority(
        listing_id: int, body: PriorityBody, client: str = Depends(owner)
    ) -> dict[str, Any]:
        async with sessions() as session, session.begin():
            listing = await session.get(Listing, listing_id)
            if listing is None:
                raise HTTPException(status_code=404, detail="no such listing")
            before, listing.priority = listing.priority, Priority(body.priority).value
        await record(
            "stock.priority", client, target=str(listing_id),
            details={"from": before, "to": body.priority},
        )  # fmt: skip
        return {"listing_id": listing_id, "priority": body.priority}

    @app.post("/api/subscription")
    async def subscription(body: SubscriptionBody, client: str = Depends(owner)) -> dict[str, Any]:
        user = require_owner_id()
        guild = await home()
        async with sessions() as session, session.begin():
            changed = await set_subscribed(session, user, body.subscribed, guild)
        if changed:
            await record("watch.subscribe" if body.subscribed else "watch.unsubscribe", client)
        return {"subscribed": body.subscribed, "changed": changed}

    # ── rules ────────────────────────────────────────────────────────────

    @app.get("/api/rules/preview")
    async def preview(
        _: str = Depends(owner),
        query: str = Query("", max_length=80),
        category: str = Query("", max_length=80),
        regions: str = Query("", max_length=100),
    ) -> dict[str, Any]:
        """How many products a rule would cover today (it also covers future ones)."""
        stmt = (
            select(Variant.sku, Product.name)
            .join(Product, Variant.product_id == Product.id)
            .join(Listing, Listing.variant_id == Variant.id)
            .where(Listing.delisted_at.is_(None))
            .distinct()
        )
        if query.strip():
            like = f"%{query.strip().lower()}%"
            stmt = stmt.where(
                or_(func.lower(Variant.sku).like(like), func.lower(Product.name).like(like))
            )
        if category.strip():
            stmt = stmt.where(func.lower(Product.category).like(f"%{category.strip().lower()}%"))
        wanted = [r.strip().lower() for r in regions.split(",") if r.strip()]
        if wanted:
            stmt = stmt.where(Listing.region.in_(wanted))
        async with sessions() as session:
            rows = (await session.execute(stmt.order_by(Variant.sku))).all()
        return {"count": len(rows), "sample": [f"{sku} · {name}" for sku, name in rows[:12]]}

    @app.get("/api/categories")
    async def categories(_: str = Depends(owner)) -> dict[str, Any]:
        """Store categories, plus family groups (a rule's category matches by prefix/substring)."""
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(Product.category, func.count())
                    .where(Product.category.is_not(None))
                    .group_by(Product.category)
                )
            ).all()
        counts = {slug: count for slug, count in rows if slug}
        return {"groups": category_groups(counts), "categories": [
            {"value": slug, "label": _label(slug), "count": count}
            for slug, count in sorted(counts.items())
        ]}  # fmt: skip

    @app.post("/api/rules")
    async def add_rule(body: RuleBody, client: str = Depends(owner)) -> dict[str, Any]:
        user = require_owner_id()
        query, category = _clean(body.query), _clean(body.category)
        regions = _regions(body.regions)
        if not (query or category or regions or body.min_profit_cents is not None):
            raise HTTPException(
                status_code=422,
                detail="Give the rule something to match (text, category, region, or minimum "
                "profit). To be pinged for everything, use 'Ping me for every alert'.",
            )
        rule = WatchRule(
            discord_user_id=user,
            guild_id=await home(),
            name=body.name.strip(),
            query=query,
            category=category,
            regions=regions,
            kinds=_kinds_or_422(body.kinds),
            max_price_cents=body.max_price_cents,
            min_profit_cents=body.min_profit_cents,
            enabled=body.enabled,
        )
        async with sessions() as session, session.begin():
            session.add(rule)
            await session.flush()
            rule_id = rule.id
            data = _rule_json(rule, owner_id())
        await record("rule.add", client, target=rule.name, details={"id": rule_id})
        return data

    @app.patch("/api/rules/{rule_id}")
    async def edit_rule(
        rule_id: int, body: EditRule, client: str = Depends(owner)
    ) -> dict[str, Any]:
        fields = body.model_fields_set
        async with sessions() as session, session.begin():
            rule = await session.get(WatchRule, rule_id)
            if rule is None:
                raise HTTPException(status_code=404, detail="no such rule")
            if "name" in fields and body.name:
                rule.name = body.name.strip()
            if "query" in fields:
                rule.query = _clean(body.query)
            if "category" in fields:
                rule.category = _clean(body.category)
            if "regions" in fields:
                rule.regions = _regions(body.regions)
            if "kinds" in fields:
                rule.kinds = _kinds_or_422(body.kinds)
            if "max_price_cents" in fields:
                rule.max_price_cents = body.max_price_cents
            if "min_profit_cents" in fields:
                rule.min_profit_cents = body.min_profit_cents
            if "enabled" in fields and body.enabled is not None:
                rule.enabled = body.enabled
            if "mute_hours" in fields:
                rule.muted_until = mute_until(body.mute_hours or 0)
            data = _rule_json(rule, owner_id())
        await record(
            "rule.update",
            client,
            target=data["name"],
            details={"id": rule_id, "fields": sorted(fields)},
        )
        return data

    @app.delete("/api/rules/{rule_id}")
    async def remove_rule(rule_id: int, client: str = Depends(owner)) -> dict[str, Any]:
        async with sessions() as session, session.begin():
            rule = await session.get(WatchRule, rule_id)
            if rule is None:
                raise HTTPException(status_code=404, detail="no such rule")
            name = rule.name
            await session.delete(rule)
        await record("rule.remove", client, target=name, details={"id": rule_id})
        return {"removed": 1}
