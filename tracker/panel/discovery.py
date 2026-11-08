"""Panel routes for the Discovery inbox (docs/DISCOVERY.md)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from tracker.db.models import Candidate, CandidateHistory, CandidateSignal
from tracker.discovery.market import decayed_heat
from tracker.discovery.service import DiscoveryService
from tracker.guilds import home_guild_id
from tracker.queries import add_watches
from tracker.watching import Resolved

Record = Callable[..., Awaitable[None]]
Resolver = Callable[[str, str | None], Awaitable[Resolved]]
STATUSES = ("promising", "new", "checked", "tracked", "ignored", "dropped")


class CheckBody(BaseModel):
    text: str = Field(min_length=2, max_length=500)


class DecisionBody(BaseModel):
    action: Literal["track", "ignore", "restore"]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def candidate_json(c: Candidate, now: datetime) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "model": c.model,
        "upc": c.upc,
        "brand": c.brand,
        "category": c.category,
        "status": c.status,
        "origin": c.origin,
        "retail_cents": c.retail_cents,
        "regular_cents": c.regular_cents,
        "retail_url": c.retail_url,
        "retail_source": c.retail_source,
        "available": c.available,
        "heat": round(decayed_heat(c.heat or 0, c.last_seen_at, now), 2),
        "signal_count": c.signal_count,
        "first_seen_at": _iso(c.first_seen_at),
        "last_seen_at": _iso(c.last_seen_at),
        "checked_at": _iso(c.checked_at),
        "next_check_at": _iso(c.next_check_at),
        "ebay_query": c.ebay_query,
        "ebay_sellers": c.ebay_sellers,
        "ebay_listings": c.ebay_listings,
        "ebay_p25_cents": c.ebay_p25_cents,
        "ebay_median_cents": c.ebay_median_cents,
        "ebay_low_cents": c.ebay_low_cents,
        "resale_cents": c.resale_cents,
        "net_cents": c.net_cents,
        "roi": c.roi,
        "confidence": c.confidence,
        "capped": c.capped,
        "score": c.score,
        "trial_until": _iso(c.trial_until),
    }


def add_discovery_routes(
    app: FastAPI,
    *,
    sessions: Any,
    settings: Any,
    discovery: DiscoveryService | None,
    owner: Callable[..., str],
    record: Record,
    owner_id: Callable[[], str | None],
    resolve: Resolver | None,
) -> None:
    def service() -> DiscoveryService:
        if discovery is None:
            raise HTTPException(status_code=409, detail="Discovery is off (DISCOVERY_ENABLED).")
        return discovery

    @app.get("/api/discovery")
    async def inbox(
        _: str = Depends(owner),
        status: str = Query("promising", max_length=12),
        q: str = Query("", max_length=80),
        limit: int = Query(100, ge=1, le=300),
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        stmt = select(Candidate).limit(limit)
        if status != "all":
            if status not in STATUSES:
                raise HTTPException(status_code=422, detail="unknown status")
            stmt = stmt.where(Candidate.status == status)
        if q.strip():
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(
                or_(func.lower(Candidate.title).like(like), func.lower(Candidate.model).like(like))
            )
        stmt = stmt.order_by(Candidate.score.desc(), Candidate.last_seen_at.desc())
        async with sessions() as session:
            rows = list(await session.scalars(stmt))
            counts = dict(
                (await session.execute(
                    select(Candidate.status, func.count()).group_by(Candidate.status)
                )).all()
            )  # fmt: skip
        used, limit_calls = discovery.budget() if discovery else (0, 0)
        return {
            "enabled": discovery is not None,
            "items": [candidate_json(c, now) for c in rows],
            "counts": {s: counts.get(s, 0) for s in STATUSES},
            "budget": {"used": used, "limit": limit_calls},
            "sources": {
                "ebay": bool(discovery and discovery.ebay_ready),
                "bestbuy": settings.bestbuy.configured,
                "bestbuy_categories": len(settings.bestbuy.categories),
            },
            "thresholds": {
                "min_profit_cents": settings.discovery.min_profit_cents,
                "min_roi": settings.discovery.min_roi,
                "min_sellers": settings.discovery.min_sellers,
                "trial_days": settings.discovery.trial_days,
            },
        }

    @app.get("/api/discovery/{candidate_id}")
    async def detail(candidate_id: int, _: str = Depends(owner)) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with sessions() as session:
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                raise HTTPException(status_code=404, detail="no such candidate")
            signals = list(
                await session.scalars(
                    select(CandidateSignal)
                    .where(CandidateSignal.candidate_id == candidate_id)
                    .order_by(CandidateSignal.seen_at.desc())
                )
            )
            history = list(
                await session.scalars(
                    select(CandidateHistory)
                    .where(CandidateHistory.candidate_id == candidate_id)
                    .order_by(CandidateHistory.day)
                )
            )
            data = candidate_json(candidate, now)
        data["signals"] = [
            {
                "source": s.source,
                "kind": s.kind,
                "title": s.title,
                "url": s.url,
                "price_cents": s.price_cents,
                "rank": s.rank,
                "times_seen": s.times_seen,
                "first_seen_at": _iso(s.first_seen_at),
                "seen_at": _iso(s.seen_at),
            }
            for s in signals
        ]
        data["history"] = [
            {
                "day": h.day,
                "sellers": h.sellers,
                "listings": h.listings,
                "p25_cents": h.p25_cents,
                "median_cents": h.median_cents,
                "retail_cents": h.retail_cents,
                "net_cents": h.net_cents,
            }
            for h in history
        ]
        return data

    @app.post("/api/discovery/check")
    async def check(body: CheckBody, client: str = Depends(owner)) -> dict[str, Any]:
        candidate_id, message = await service().manual(body.text)
        await record(
            "discovery.check", client, target=body.text[:200], details={"id": candidate_id}
        )
        if candidate_id is None:
            raise HTTPException(status_code=422, detail=message or "Couldn't check that.")
        return {"id": candidate_id, "message": message}

    @app.post("/api/discovery/{candidate_id}/recheck")
    async def recheck(candidate_id: int, client: str = Depends(owner)) -> dict[str, Any]:
        result = await service().check(candidate_id)
        if result is None:
            raise HTTPException(
                status_code=409, detail="Not checked: eBay unavailable or today's budget is used."
            )
        await record("discovery.recheck", client, target=result.title[:200])
        return candidate_json(result, datetime.now(UTC))

    @app.post("/api/discovery/{candidate_id}/decision")
    async def decision(
        candidate_id: int, body: DecisionBody, client: str = Depends(owner)
    ) -> dict[str, Any]:
        candidate = await service().decide(candidate_id, body.action)
        if candidate is None:
            raise HTTPException(status_code=404, detail="no such candidate")
        watched = 0
        note = None
        if body.action == "track" and candidate.retail_url:
            user = owner_id()
            if user and resolve is not None:
                resolved = await resolve(candidate.retail_url, None)
                if resolved.rows:
                    async with sessions() as session, session.begin():
                        home = await home_guild_id(session, settings.discord)
                        await add_watches(
                            session, user, [r.listing.id for r in resolved.rows], None, home
                        )
                    watched = len(resolved.rows)
                else:
                    note = resolved.error
        await record(
            f"discovery.{body.action}",
            client,
            target=candidate.title[:200],
            details={"id": candidate_id, "watched": watched},
        )
        return {
            "id": candidate_id,
            "status": candidate.status,
            "watched": watched,
            "note": note,
        }
