"""Discord servers ("guilds"): allowlist and per-server alert configuration.

Only `approved` servers get alerts or accept commands. Servers listed in config
(DISCORD_GUILD_ID, DISCORD_ALLOWED_GUILD_IDS) are approved at startup; any other server the
bot is added to stays `pending` until the owner approves it, and is left otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tracker.audit import audit
from tracker.db.models import Guild
from tracker.settings import DiscordSettings


class GuildStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    BLOCKED = "blocked"


# Settings a server admin may change. `show_valuations` is owner-only (eBay license).
ADMIN_FIELDS = frozenset(
    {
        "alert_channel_id",
        "radar_channel_id",
        "alert_mention",
        "admin_role_id",
        "mention_kinds",
        "cooldown_minutes",
        "digest_threshold",
    }
)
OWNER_FIELDS = ADMIN_FIELDS | {"show_valuations"}


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


@dataclass(frozen=True, slots=True)
class GuildConfig:
    """Effective configuration for one server (row values over global defaults)."""

    guild_id: str
    name: str | None = None
    status: str = GuildStatus.APPROVED
    alert_channel_id: int | None = None
    radar_channel_id: int | None = None
    alert_mention: str = ""
    admin_role_id: int | None = None
    mention_kinds: frozenset[str] = field(default_factory=frozenset)
    cooldown_minutes: float = 10.0
    digest_threshold: int = 5
    show_valuations: bool = False
    is_home: bool = False

    @property
    def approved(self) -> bool:
        return self.status == GuildStatus.APPROVED

    @property
    def effective_radar_channel(self) -> int | None:
        return self.radar_channel_id or self.alert_channel_id

    @classmethod
    def from_row(
        cls, row: Guild, defaults: DiscordSettings, home_guild_id: str | None
    ) -> GuildConfig:
        kinds = (
            {k.strip() for k in row.mention_kinds.split(",") if k.strip()}
            if row.mention_kinds is not None
            else set(defaults.mention_kinds)
        )
        return cls(
            guild_id=row.guild_id,
            name=row.name,
            status=row.status,
            alert_channel_id=_int(row.alert_channel_id),
            radar_channel_id=_int(row.radar_channel_id),
            alert_mention=row.alert_mention or "",
            admin_role_id=_int(row.admin_role_id),
            mention_kinds=frozenset(kinds),
            cooldown_minutes=(
                row.cooldown_minutes
                if row.cooldown_minutes is not None
                else defaults.alert_cooldown_minutes
            ),
            digest_threshold=row.digest_threshold or defaults.digest_threshold,
            show_valuations=bool(row.show_valuations),
            is_home=row.guild_id == home_guild_id,
        )

    @classmethod
    def from_settings(cls, settings: DiscordSettings, guild_id: str = "global") -> GuildConfig:
        """Config built purely from env settings (SMS routing, single-server installs, tests)."""
        return cls(
            guild_id=guild_id,
            alert_channel_id=settings.channel_id,
            radar_channel_id=settings.radar_channel_id,
            alert_mention=settings.alert_mention.strip(),
            mention_kinds=frozenset(settings.mention_kinds),
            cooldown_minutes=settings.alert_cooldown_minutes,
            digest_threshold=settings.digest_threshold,
            show_valuations=True,
            is_home=True,
        )


async def get_guild(session: AsyncSession, guild_id: str | int) -> Guild | None:
    return await session.get(Guild, str(guild_id))


async def home_guild_id(session: AsyncSession, settings: DiscordSettings) -> str | None:
    """The server legacy (pre-multi-server) watches belong to."""
    if settings.guild_id:
        return str(settings.guild_id)
    first = await session.scalar(
        select(Guild.guild_id)
        .where(Guild.status == GuildStatus.APPROVED)
        .order_by(Guild.decided_at, Guild.guild_id)
        .limit(1)
    )
    return first


async def approved_configs(session: AsyncSession, settings: DiscordSettings) -> list[GuildConfig]:
    home = await home_guild_id(session, settings)
    rows = await session.scalars(select(Guild).where(Guild.status == GuildStatus.APPROVED))
    return [GuildConfig.from_row(row, settings, home) for row in rows]


async def config_for(
    session: AsyncSession, settings: DiscordSettings, guild_id: str | int
) -> GuildConfig | None:
    row = await get_guild(session, guild_id)
    if row is None:
        return None
    return GuildConfig.from_row(row, settings, await home_guild_id(session, settings))


async def ensure_guild(
    session: AsyncSession, guild_id: str | int, name: str | None = None
) -> tuple[Guild, bool]:
    """Get or create the row (new servers start `pending`). Returns (row, created)."""
    row = await get_guild(session, guild_id)
    if row is None:
        row = Guild(guild_id=str(guild_id), name=name, status=GuildStatus.PENDING)
        session.add(row)
        return row, True
    if name and row.name != name:
        row.name = name
    return row, False


async def decide(
    session: AsyncSession,
    guild_id: str | int,
    status: GuildStatus,
    *,
    actor_id: str | None,
    actor_name: str | None = None,
    name: str | None = None,
) -> Guild:
    row, _ = await ensure_guild(session, guild_id, name)
    if row.status != status:
        await audit(
            session,
            f"guild.{status.value}",
            actor_id=actor_id,
            actor_name=actor_name,
            guild_id=row.guild_id,
            target=row.name or row.guild_id,
            details={"from": row.status, "to": status.value},
        )
        row.status = status.value
        row.decided_at = datetime.now(UTC)
        row.decided_by = actor_id
    return row


async def bootstrap(session: AsyncSession, settings: DiscordSettings) -> list[str]:
    """Approve config-listed servers; seed the home server from the env channel settings."""
    approved: list[str] = []
    for guild_id in settings.preapproved_guilds:
        row = await decide(session, guild_id, GuildStatus.APPROVED, actor_id=None)
        approved.append(row.guild_id)
    if settings.guild_id:
        await seed_home(session, str(settings.guild_id), settings)
    return approved


async def seed_home(session: AsyncSession, guild_id: str, settings: DiscordSettings) -> Guild:
    """Copy env channel config into the home server row (only fields not set yet)."""
    row, _ = await ensure_guild(session, guild_id)
    seeded: dict[str, Any] = {}
    if row.alert_channel_id is None and settings.channel_id:
        row.alert_channel_id = seeded["alert_channel_id"] = str(settings.channel_id)
    if row.radar_channel_id is None and settings.radar_channel_id:
        row.radar_channel_id = seeded["radar_channel_id"] = str(settings.radar_channel_id)
    if row.alert_mention is None and settings.alert_mention.strip():
        row.alert_mention = seeded["alert_mention"] = settings.alert_mention.strip()
    if seeded and not row.show_valuations:
        # The home server is the owner's own; valuations are on there by default.
        row.show_valuations = seeded["show_valuations"] = True
    if seeded:
        await audit(
            session, "guild.seed_from_env", guild_id=guild_id, target=guild_id, details=seeded
        )
    return row


async def update_config(
    session: AsyncSession,
    guild_id: str | int,
    changes: dict[str, Any],
    *,
    allowed: frozenset[str],
    actor_id: str,
    actor_name: str | None = None,
) -> Guild:
    """Apply validated changes; refuses fields the caller's role may not touch."""
    forbidden = set(changes) - allowed
    if forbidden:
        raise PermissionError(f"not allowed to change: {', '.join(sorted(forbidden))}")
    row = await get_guild(session, guild_id)
    if row is None or row.status != GuildStatus.APPROVED:
        raise LookupError("server is not approved")
    before = {key: getattr(row, key) for key in changes}
    for key, value in changes.items():
        setattr(row, key, value)
    after = {key: getattr(row, key) for key in changes}
    if before != after:
        await audit(
            session,
            "guild.configure",
            actor_id=actor_id,
            actor_name=actor_name,
            guild_id=row.guild_id,
            target=row.name or row.guild_id,
            details={"before": before, "after": after},
        )
    return row


async def list_guilds(session: AsyncSession) -> list[Guild]:
    return list(await session.scalars(select(Guild).order_by(Guild.status, Guild.name)))
