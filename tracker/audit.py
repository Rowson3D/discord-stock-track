"""Append-only audit trail of administrative actions (the database rejects UPDATE/DELETE)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.db.models import AuditLog


async def audit(
    session: AsyncSession,
    action: str,
    *,
    actor_id: str | int | None = None,
    actor_name: str | None = None,
    guild_id: str | int | None = None,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Record an action. `actor_id=None` means the system acted on its own."""
    entry = AuditLog(
        at=datetime.now(UTC),
        actor_id=None if actor_id is None else str(actor_id),
        actor_name=(actor_name or "")[:100] or None,
        guild_id=None if guild_id is None else str(guild_id),
        action=action,
        target=(target or "")[:200] or None,
        details=json.dumps(details, default=str, sort_keys=True) if details else None,
    )
    session.add(entry)
    return entry


async def recent_audit(
    session: AsyncSession, *, limit: int = 25, guild_id: str | None = None
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if guild_id is not None:
        stmt = stmt.where(AuditLog.guild_id == guild_id)
    return list(await session.scalars(stmt))


def describe(entry: AuditLog) -> str:
    who = entry.actor_name or entry.actor_id or "system"
    parts = [f"{entry.at:%Y-%m-%d %H:%M} UTC", who, entry.action]
    if entry.target:
        parts.append(entry.target)
    if entry.details:
        parts.append(entry.details[:300])
    return " · ".join(parts)
