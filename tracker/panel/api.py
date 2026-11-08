"""Owner control panel: a JSON API plus a no-build static web app, served by `tracker run`.

Security model (see docs/PANEL.md):
- Binds to localhost only; remote access goes through `tailscale serve` (tailnet only).
- Owner-only: TOTP login, short-lived server-side sessions, lockout on failed codes.
- Host-header allowlist (stops DNS rebinding), optional Tailscale login allowlist.
- Writes need a custom header plus a same-origin Origin, and cookies are SameSite=Strict (CSRF).
- Strict CSP with no inline script or style, and no third-party code at all.
- Secrets are never returned; sources show only "configured" flags.
- Every login, logout, and change is written to the append-only audit log.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker import __version__
from tracker.audit import audit, recent_audit
from tracker.db.models import EbayStatsHistory, Event, Listing, Product, Valuation, Variant
from tracker.discovery.service import DiscoveryService
from tracker.guilds import GuildStatus, decide, list_guilds
from tracker.history import ebay_trend, stock_history
from tracker.outbox import load_events
from tracker.panel.auth import LoginLimiter, SessionStore, TotpVerifier
from tracker.panel.discovery import add_discovery_routes
from tracker.panel.watchlist import Resolver, add_watchlist_routes
from tracker.queries import opportunities, resolve_variant, search_variants, tracker_stats
from tracker.scheduler import Scheduler
from tracker.settings import Settings
from tracker.watchtest import AlertPoster

STATIC_DIR = Path(__file__).resolve().parent / "static"
MUTATION_HEADER = "x-flipscout"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

GuildDecider = Callable[[str, GuildStatus, str | None, str], Awaitable[None]]


@dataclass(slots=True)
class PanelDeps:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    started_at: datetime
    scheduler: Scheduler | None = None
    # The bot's decision hook (syncs commands / leaves the server); DB-only when absent.
    decide_guild: GuildDecider | None = None
    # Resolves SKUs, names, and store links (fetching new links); catalog-only when absent.
    resolve: Resolver | None = None
    # Current owner IDs (the bot learns them from Discord when .env doesn't list any).
    owner_ids: Callable[[], Iterable[str]] | None = None
    discovery: DiscoveryService | None = None
    # Posts watch test alerts to Discord; tests only explain when absent.
    alerts: AlertPoster | None = None
    clock: Callable[[], float] = field(default=time.time)


class LoginBody(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class DecisionBody(BaseModel):
    status: Literal["approved", "blocked"]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _valuation(v: Valuation | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {
        "computed_at": _iso(v.computed_at),
        "retail_cents": v.retail_cents,
        "available": v.available,
        "basis": v.basis,
        "resale_cents": v.resale_cents,
        "net_cents": v.net_cents,
        "roi": v.roi,
        "fees_cents": v.fees_cents,
        "shipping_cents": v.shipping_cents,
        "cost_cents": v.cost_cents,
        "confidence": v.confidence,
        "active_count": v.active_count,
        "active_median_cents": v.active_median_cents,
        "active_low_cents": v.active_low_cents,
        "sold_count": v.sold_count,
        "sold_median_cents": v.sold_median_cents,
        "manual_count": v.manual_count,
        "sell_through": v.sell_through,
        "score": v.score,
        "deal_active": v.deal_active,
        "scarcity_days": v.scarcity_days,
        "scarcity_resale_cents": v.scarcity_resale_cents,
        "scarcity_net_cents": v.scarcity_net_cents,
        "scarcity_roi": v.scarcity_roi,
        "flip_basis": v.flip_basis,
        "flip_net_cents": v.flip_net_cents,
        "flip_roi": v.flip_roi,
        "flip_confidence": v.flip_confidence,
    }


def _item(variant: Variant, product: Product, listing: Listing | None) -> dict[str, Any]:
    return {
        "variant_id": variant.id,
        "sku": variant.sku,
        "name": product.name,
        "category": product.category,
        "pack_size": variant.pack_size,
        "region": listing.region if listing else None,
        "status": listing.status if listing else None,
        "url": listing.url if listing else None,
        "early_access": bool(listing and listing.is_early_access),
    }


def create_app(deps: PanelDeps) -> FastAPI:
    panel = deps.settings.panel
    secret = panel.totp_secret.get_secret_value() if panel.totp_secret else ""
    if not secret:
        raise ValueError("Panel needs PANEL_TOTP_SECRET (run `tracker panel setup`).")
    verifier = TotpVerifier(secret, clock=deps.clock)
    store = SessionStore(panel.session_hours * 3600, clock=deps.clock)
    limiter = LoginLimiter(clock=deps.clock)
    cookie_name = "__Host-flipscout" if panel.secure_cookies else "flipscout"
    hosts = {f"{name}:{panel.port}" for name in ("127.0.0.1", "localhost", "[::1]")} | {
        h.lower() for h in panel.allowed_hosts
    }
    logins = {login.lower() for login in panel.tailscale_logins}

    def owner_id() -> str | None:
        configured = deps.settings.discord.owner_ids
        if configured:
            return str(configured[0])
        learned = sorted(deps.owner_ids()) if deps.owner_ids is not None else []
        return learned[0] if learned else None

    app = FastAPI(
        title="FlipScout panel",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.sessions_store = store

    def client_of(request: Request) -> str:
        login = request.headers.get("tailscale-user-login")
        if login:
            return f"tailscale:{login.lower()}"
        return request.client.host if request.client else "unknown"

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        host = request.headers.get("host", "").lower()
        if host not in hosts:
            # DNS rebinding or an unexpected proxy: refuse before touching any route.
            return JSONResponse({"error": "unrecognised host"}, status_code=421)
        login = request.headers.get("tailscale-user-login")
        if logins and login is not None and login.lower() not in logins:
            return JSONResponse({"error": "tailnet user not allowed"}, status_code=403)
        if request.method not in SAFE_METHODS:
            if request.headers.get(MUTATION_HEADER) != "1":
                return JSONResponse({"error": "missing panel header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != host:
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            # Revalidate every load (cheap: ETag), so an update never runs stale code.
            response.headers["Cache-Control"] = "no-cache"
        return response

    def owner(request: Request) -> str:
        if store.get(request.cookies.get(cookie_name)) is None:
            raise HTTPException(status_code=401, detail="login required")
        return client_of(request)

    async def record(action: str, client: str, **kwargs: Any) -> None:
        async with deps.sessions() as session, session.begin():
            await audit(
                session,
                action,
                actor_id=owner_id(),
                actor_name=f"owner (panel, {client})"[:100],
                **kwargs,
            )

    # ── session ──────────────────────────────────────────────────────────

    @app.get("/api/session")
    async def session_info(request: Request) -> dict[str, Any]:
        current = store.get(request.cookies.get(cookie_name))
        return {
            "authenticated": current is not None,
            "expires_at": (_iso(datetime.fromtimestamp(current.expires, UTC)) if current else None),
        }

    @app.post("/api/session")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        client = client_of(request)
        if limiter.blocked(client):
            raise HTTPException(status_code=429, detail="too many attempts; wait 15 minutes")
        if not verifier.verify(body.code):
            limiter.failed(client)
            await record("panel.login_failed", client)
            raise HTTPException(status_code=401, detail="wrong or expired code")
        limiter.succeeded(client)
        token, current = store.create(client)
        response.set_cookie(
            cookie_name,
            token,
            max_age=int(current.expires - current.created),
            httponly=True,
            secure=panel.secure_cookies,
            samesite="strict",
            path="/",
        )
        await record("panel.login", client)
        return {
            "authenticated": True,
            "expires_at": _iso(datetime.fromtimestamp(current.expires, UTC)),
        }

    @app.delete("/api/session")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(cookie_name)
        if store.get(token) is not None:
            store.revoke(token)
            await record("panel.logout", client_of(request))
        response.delete_cookie(
            cookie_name, path="/", secure=panel.secure_cookies, httponly=True, samesite="strict"
        )
        return {"authenticated": False}

    @app.post("/api/session/revoke-all")
    async def revoke_all(response: Response, client: str = Depends(owner)) -> dict[str, Any]:
        store.revoke_all()
        await record("panel.revoke_all", client)
        response.delete_cookie(
            cookie_name, path="/", secure=panel.secure_cookies, httponly=True, samesite="strict"
        )
        return {"authenticated": False}

    # ── data ─────────────────────────────────────────────────────────────

    @app.get("/api/overview")
    async def overview(_: str = Depends(owner)) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with deps.sessions() as session:
            stats = await tracker_stats(session, now)
            bases = dict(
                (await session.execute(
                    select(Valuation.basis, func.count()).group_by(Valuation.basis)
                )).all()
            )  # fmt: skip
            flips = await session.scalar(
                select(func.count()).where(Valuation.flip_basis.is_not(None))
            )
            deals = await session.scalar(select(func.count()).where(Valuation.deal_active))
            sold_out_market = await session.scalar(
                select(func.count()).where(
                    Valuation.available.is_(False), Valuation.basis.is_not(None)
                )
            )
            uncapped = await session.scalar(
                select(func.count()).where(Valuation.available, Valuation.basis == "active")
            )
        regions: dict[str, dict[str, Any]] = {}
        for (region, status), count in stats.status_counts.items():
            entry = regions.setdefault(region, {"counts": {}, "last_checked": None})
            entry["counts"][status] = count
        for region, checked in stats.last_checked.items():
            regions.setdefault(region, {"counts": {}, "last_checked": None})["last_checked"] = _iso(
                checked
            )
        jobs = []
        if deps.scheduler is not None:
            mono = time.monotonic()
            for job in deps.scheduler.jobs.values():
                jobs.append(
                    {
                        "name": job.name,
                        "runs": job.runs,
                        "failures": job.failures,
                        "running": job.running,
                        "next_in_seconds": max(0, int(job.next_run - mono)),
                        "interval_seconds": job.interval,
                    }
                )
        s = deps.settings
        return {
            "version": __version__,
            "started_at": _iso(deps.started_at),
            "uptime_seconds": int((now - deps.started_at).total_seconds()),
            "regions": regions,
            "watches": stats.watches,
            "subscribers": stats.subscribers,
            "events_24h": stats.events_24h,
            "pending_events": stats.pending_events,
            "valuations": {str(k or "none"): v for k, v in bases.items()},
            "sold_out_market": sold_out_market or 0,
            "in_stock_under_cap": uncapped or 0,
            "credible_flips": flips or 0,
            "active_deals": deals or 0,
            "jobs": jobs,
            "sources": {  # Configured or not; never the values themselves.
                "discord": s.discord.enabled,
                "ebay": s.ebay.configured,
                "sms": s.sms.configured,
                "unifi_regions": list(s.unifi.regions),
                "tailscale_logins": bool(s.panel.tailscale_logins),
            },
        }

    @app.get("/api/opportunities")
    async def list_opportunities(
        _: str = Depends(owner),
        limit: int = Query(50, ge=1, le=200),
        available: bool = False,
    ) -> dict[str, Any]:
        async with deps.sessions() as session:
            rows = await opportunities(session, limit=limit, available_only=available)
        return {
            "items": [
                {**_item(r.variant, r.product, r.listing), "valuation": _valuation(r.valuation)}
                for r in rows
            ]
        }

    @app.get("/api/market")
    async def market(
        _: str = Depends(owner),
        view: Literal["sold_out", "capped", "flips", "all"] = "sold_out",
        q: str = Query("", max_length=60),
        limit: int = Query(150, ge=1, le=500),
    ) -> dict[str, Any]:
        stmt = (
            select(Valuation, Variant, Product, Listing)
            .join(Variant, Valuation.variant_id == Variant.id)
            .join(Product, Variant.product_id == Product.id)
            .outerjoin(Listing, Valuation.listing_id == Listing.id)
            .limit(limit)
        )
        if view == "sold_out":
            stmt = stmt.where(
                Valuation.available.is_(False), Valuation.basis.is_not(None)
            ).order_by(Valuation.net_cents.desc().nulls_last())
        elif view == "capped":
            premium = Valuation.active_median_cents - Valuation.retail_cents
            stmt = stmt.where(Valuation.basis == "capped").order_by(premium.desc().nulls_last())
        elif view == "flips":
            stmt = stmt.where(Valuation.flip_basis.is_not(None)).order_by(
                Valuation.score.desc(), Valuation.flip_net_cents.desc()
            )
        else:
            stmt = stmt.where(Valuation.basis.is_not(None)).order_by(
                Valuation.score.desc(), Valuation.net_cents.desc().nulls_last()
            )
        text = q.strip().lower()
        if text:
            like = f"%{text}%"
            stmt = stmt.where(or_(Variant.sku_key.like(like), func.lower(Product.name).like(like)))
        async with deps.sessions() as session:
            rows = (await session.execute(stmt)).all()
        return {
            "view": view,
            "items": [
                {**_item(variant, product, listing), "valuation": _valuation(valuation)}
                for valuation, variant, product, listing in rows
            ],
        }

    @app.get("/api/search")
    async def search(_: str = Depends(owner), q: str = Query("", max_length=60)) -> dict[str, Any]:
        async with deps.sessions() as session:
            matches = await search_variants(session, q, limit=15)
        return {
            "items": [
                {"variant_id": m.variant_id, "sku": m.sku, "name": m.product_name,
                 "pack_size": m.pack_size}
                for m in matches
            ]
        }  # fmt: skip

    @app.get("/api/products/{key}")
    async def product(key: str, _: str = Depends(owner)) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with deps.sessions() as session:
            variant: Variant | None
            if key.isdigit():
                variant = await session.get(Variant, int(key))
            else:
                variant = await resolve_variant(session, key[:60])
            if variant is None:
                raise HTTPException(status_code=404, detail="no such product")
            item = await session.get(Product, variant.product_id)
            assert item is not None
            rows = await session.scalars(
                select(Listing).where(Listing.variant_id == variant.id).order_by(Listing.region)
            )
            listings = []
            for listing in rows:
                history = await stock_history(session, listing.id)
                out_for = history.out_of_stock_for(now)
                listings.append(
                    {
                        "id": listing.id,
                        "region": listing.region,
                        "status": listing.status,
                        "raw_status": listing.raw_status,
                        "price_cents": listing.price_cents,
                        "currency": listing.currency,
                        "url": listing.url,
                        "priority": listing.priority,
                        "early_access": listing.is_early_access,
                        "restock_eta": listing.restock_eta,
                        "last_checked_at": _iso(listing.last_checked_at),
                        "last_in_stock_at": _iso(listing.last_in_stock_at),
                        "delisted_at": _iso(listing.delisted_at),
                        "first_seen": _iso(history.first_seen),
                        "restocks": history.restocks,
                        "median_sellout_seconds": (
                            int(history.median_sellout.total_seconds())
                            if history.median_sellout
                            else None
                        ),
                        "out_of_stock_seconds": int(out_for.total_seconds()) if out_for else None,
                        "out_of_stock_at_least": history.unavailable_at_least,
                    }
                )
            valuation = await session.get(Valuation, variant.id)
            snapshots = list(
                await session.scalars(
                    select(EbayStatsHistory)
                    .where(
                        EbayStatsHistory.variant_id == variant.id,
                        EbayStatsHistory.day >= (now - timedelta(days=180)).date(),
                    )
                    .order_by(EbayStatsHistory.day)
                )
            )
            trend = await ebay_trend(session, variant.id, days=7)
            ids = [entry["id"] for entry in listings]
            events = (
                list(
                    await session.scalars(
                        select(Event)
                        .where(Event.listing_id.in_(ids))
                        .order_by(Event.id.desc())
                        .limit(40)
                    )
                )
                if ids
                else []
            )
            region_of = {entry["id"]: entry["region"] for entry in listings}
        return {
            "variant_id": variant.id,
            "sku": variant.sku,
            "name": item.name,
            "category": item.category,
            "pack_size": variant.pack_size,
            "listings": listings,
            "valuation": _valuation(valuation),
            "ebay_history": [
                {
                    "day": s.day,
                    "sellers": s.active_count,
                    "listings": s.active_listings,
                    "p25_cents": s.active_p25_cents,
                    "median_cents": s.active_median_cents,
                    "retail_cents": s.retail_cents,
                    "sold_median_cents": s.sold_median_cents,
                }
                for s in snapshots
            ],
            "trend": (
                {
                    "days": trend.days,
                    "sellers_then": trend.sellers_then,
                    "sellers_now": trend.sellers_now,
                    "sellers_change": trend.sellers_change,
                    "p25_then": trend.p25_then,
                    "p25_now": trend.p25_now,
                    "price_change": trend.price_change,
                }
                if trend
                else None
            ),
            "events": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "occurred_at": _iso(e.occurred_at),
                    "region": region_of.get(e.listing_id or -1),
                    "old_value": e.old_value,
                    "new_value": e.new_value,
                }
                for e in events
            ],
        }

    @app.get("/api/events")
    async def events(
        _: str = Depends(owner),
        limit: int = Query(100, ge=1, le=500),
        kind: list[str] | None = Query(None),
    ) -> dict[str, Any]:
        async with deps.sessions() as session:
            rows = await load_events(session, pending_only=False, limit=limit, kinds=kind or None)
        return {
            "items": [
                {
                    "id": e.id,
                    "kind": e.kind.value,
                    "occurred_at": _iso(e.occurred_at),
                    "sku": e.sku,
                    "name": e.product_name,
                    "region": e.region,
                    "status": e.status.value,
                    "price_cents": e.price_cents,
                    "currency": e.currency,
                    "old_value": e.old_value,
                    "new_value": e.new_value,
                    "pack_size": e.pack_size,
                    "early_access": e.is_early_access,
                    "url": e.url,
                    "profit_text": e.profit_text,
                    "summary": e.summary(),
                }
                for e in rows
            ]
        }

    @app.get("/api/servers")
    async def servers(_: str = Depends(owner)) -> dict[str, Any]:
        async with deps.sessions() as session:
            rows = await list_guilds(session)
        return {
            "items": [
                {
                    "guild_id": g.guild_id,
                    "name": g.name,
                    "status": g.status,
                    "decided_at": _iso(g.decided_at),
                    "show_valuations": g.show_valuations,
                    "alert_channel": bool(g.alert_channel_id),
                    "radar_channel": bool(g.radar_channel_id),
                    "created_at": _iso(g.created_at),
                }
                for g in rows
            ]
        }

    @app.post("/api/servers/{guild_id}/decision")
    async def server_decision(
        guild_id: str, body: DecisionBody, client: str = Depends(owner)
    ) -> dict[str, Any]:
        if not guild_id.isdigit() or len(guild_id) > 25:
            raise HTTPException(status_code=422, detail="server IDs are numbers")
        status = GuildStatus(body.status)
        actor_name = f"owner (panel, {client})"[:100]
        if deps.decide_guild is not None:
            await deps.decide_guild(guild_id, status, owner_id(), actor_name)
        else:
            async with deps.sessions() as session, session.begin():
                await decide(session, guild_id, status, actor_id=owner_id(), actor_name=actor_name)
        return {"guild_id": guild_id, "status": status.value}

    @app.get("/api/audit")
    async def audit_log(
        _: str = Depends(owner), limit: int = Query(100, ge=1, le=500)
    ) -> dict[str, Any]:
        async with deps.sessions() as session:
            rows = await recent_audit(session, limit=limit)
        items = []
        for entry in rows:
            try:
                details = json.loads(entry.details) if entry.details else None
            except ValueError:
                details = entry.details
            items.append(
                {
                    "id": entry.id,
                    "at": _iso(entry.at),
                    "actor": entry.actor_name or entry.actor_id or "system",
                    "guild_id": entry.guild_id,
                    "action": entry.action,
                    "target": entry.target,
                    "details": details,
                }
            )
        return {"items": items}

    add_watchlist_routes(
        app,
        sessions=deps.sessions,
        settings=deps.settings,
        owner=owner,
        record=record,
        owner_id=owner_id,
        resolve=deps.resolve,
        alerts=deps.alerts,
        clock=lambda: datetime.fromtimestamp(deps.clock(), UTC),
    )

    add_discovery_routes(
        app,
        sessions=deps.sessions,
        settings=deps.settings,
        discovery=deps.discovery,
        owner=owner,
        record=record,
        owner_id=owner_id,
        resolve=deps.resolve,
    )

    # ── web app ──────────────────────────────────────────────────────────

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
