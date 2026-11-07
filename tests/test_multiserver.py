"""Phase 4: servers allowlist, roles, per-server routing, audit log, privacy, redaction."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import T0, make_snapshot
from tracker.alerts import AlertRouter
from tracker.audit import audit, recent_audit
from tracker.bot.sink import DiscordSink
from tracker.db.migrate import upgrade
from tracker.db.models import AuditLog, Guild, Listing, MarketComp, Valuation, Watch
from tracker.domain import EventKind, StockStatus
from tracker.guilds import (
    ADMIN_FIELDS,
    OWNER_FIELDS,
    GuildConfig,
    GuildStatus,
    approved_configs,
    bootstrap,
    config_for,
    decide,
    ensure_guild,
    home_guild_id,
    update_config,
)
from tracker.ingest import Ingestor
from tracker.logconfig import MASK, Redactor, configure_logging
from tracker.outbox import load_events
from tracker.permissions import Role, check_access, resolve_role
from tracker.queries import add_watches, forget_user, set_subscribed
from tracker.settings import DiscordSettings

HOME, OTHER = "111", "222"


def discord(**overrides: object) -> DiscordSettings:
    values: dict[str, object] = {"guild_id": int(HOME), "channel_id": 9001}
    values.update(overrides)
    return DiscordSettings(_env_file=None, **values)  # type: ignore[call-arg]


def approved(guild_id: str = HOME, **kw: Any) -> GuildConfig:
    return GuildConfig(guild_id=guild_id, status=GuildStatus.APPROVED, **kw)


# ── permissions ───────────────────────────────────────────────────────────


def test_role_resolution():
    guild = approved(admin_role_id=77)
    assert resolve_role("1", owner_ids={"1"}, guild=guild) is Role.OWNER
    assert resolve_role("2", owner_ids={"1"}, manage_guild=True, guild=guild) is Role.SERVER_ADMIN
    assert resolve_role("3", owner_ids={"1"}, role_ids=[77], guild=guild) is Role.SERVER_ADMIN
    assert (
        resolve_role("4", owner_ids={"1"}, global_admin_ids=["4"], guild=guild) is Role.SERVER_ADMIN
    )
    assert resolve_role("5", owner_ids={"1"}, role_ids=[78], guild=guild) is Role.MEMBER
    # Manage Server in an unapproved server grants nothing.
    pending = GuildConfig(guild_id="9", status=GuildStatus.PENDING)
    assert resolve_role("2", owner_ids=set(), manage_guild=True, guild=pending) is Role.MEMBER


@pytest.mark.parametrize(
    ("command", "role", "guild", "allowed"),
    [
        ("watch", Role.MEMBER, approved(), True),
        ("watch", Role.OWNER, None, False),  # DMs / unknown servers: nothing works
        ("watch", Role.MEMBER, GuildConfig(guild_id="9", status=GuildStatus.PENDING), False),
        ("check", Role.MEMBER, approved(), False),
        ("check", Role.SERVER_ADMIN, approved(), True),
        ("server configure", Role.SERVER_ADMIN, approved(), True),
        ("server approve", Role.SERVER_ADMIN, approved(), False),
        ("server approve", Role.OWNER, approved(), True),
        ("comp", Role.SERVER_ADMIN, approved(), False),
        ("opportunities", Role.MEMBER, approved(show_valuations=False), False),
        ("opportunities", Role.MEMBER, approved(show_valuations=True), True),
        ("opportunities", Role.OWNER, approved(show_valuations=False), True),
        ("some-new-command", Role.SERVER_ADMIN, approved(), False),  # default: owner only
    ],
)
def test_access_matrix(command: str, role: Role, guild: GuildConfig | None, allowed: bool):
    assert (check_access(command, role, guild) is None) is allowed


# ── servers ───────────────────────────────────────────────────────────────


async def test_bootstrap_approves_and_seeds_home(sessions: async_sessionmaker[AsyncSession]):
    settings = discord(allowed_guild_ids=OTHER, alert_mention="<@&5>")
    async with sessions() as session, session.begin():
        assert await bootstrap(session, settings) == [HOME, OTHER]
        assert await bootstrap(session, settings) == [HOME, OTHER]  # idempotent
    async with sessions() as session:
        configs = {c.guild_id: c for c in await approved_configs(session, settings)}
        assert set(configs) == {HOME, OTHER}
        home = configs[HOME]
        assert home.alert_channel_id == 9001
        assert home.alert_mention == "<@&5>"
        assert home.show_valuations
        assert home.is_home
        assert not configs[OTHER].show_valuations  # other servers start without valuations
        assert configs[OTHER].alert_channel_id is None
        actions = [e.action for e in await recent_audit(session, limit=10)]
        assert actions.count("guild.approved") == 2  # not repeated by the second bootstrap
        assert "guild.seed_from_env" in actions


async def test_new_server_is_pending_and_decisions_are_audited(sessions):
    async with sessions() as session, session.begin():
        row, created = await ensure_guild(session, "333", "Stranger")
        assert created
        assert row.status == GuildStatus.PENDING
        await decide(session, "333", GuildStatus.BLOCKED, actor_id="1", actor_name="owner")
    async with sessions() as session:
        [entry] = await recent_audit(session, guild_id="333")
        assert entry.action == "guild.blocked"
        assert entry.actor_id == "1"
        assert await config_for(session, discord(), "333") is not None
        assert await approved_configs(session, discord(guild_id=None)) == []


async def test_server_admins_cannot_enable_valuations(sessions):
    async with sessions() as session, session.begin():
        await decide(session, OTHER, GuildStatus.APPROVED, actor_id=None)
        with pytest.raises(PermissionError):
            await update_config(
                session, OTHER, {"show_valuations": True}, allowed=ADMIN_FIELDS, actor_id="2"
            )
        await update_config(
            session, OTHER, {"cooldown_minutes": 3.0}, allowed=ADMIN_FIELDS, actor_id="2"
        )
        await update_config(
            session, OTHER, {"show_valuations": True}, allowed=OWNER_FIELDS, actor_id="1"
        )
        with pytest.raises(LookupError):
            await update_config(
                session, "999", {"cooldown_minutes": 1.0}, allowed=ADMIN_FIELDS, actor_id="1"
            )
    async with sessions() as session:
        config = await config_for(session, discord(), OTHER)
        assert config.cooldown_minutes == 3.0
        assert config.show_valuations
        entry = (await recent_audit(session, guild_id=OTHER))[0]
        assert entry.action == "guild.configure"
        assert '"show_valuations": true' in entry.details


async def test_home_guild_falls_back_to_first_approved(sessions):
    async with sessions() as session, session.begin():
        await decide(session, OTHER, GuildStatus.APPROVED, actor_id=None)
    async with sessions() as session:
        assert await home_guild_id(session, discord()) == HOME  # configured wins
        assert await home_guild_id(session, discord(guild_id=None)) == OTHER


# ── audit log is append-only in the database ──────────────────────────────


async def test_audit_log_rejects_update_and_delete(sessions):
    async with sessions() as session, session.begin():
        await audit(session, "test.action", actor_id="1", details={"a": 1})
    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        async with sessions() as session:
            with pytest.raises(DatabaseError, match="append-only"):
                await session.execute(text(statement))
    async with sessions() as session:
        assert len((await session.scalars(select(AuditLog))).all()) == 1


def test_migrated_database_also_enforces_append_only(tmp_path: Path):
    db = (tmp_path / "m.db").as_posix()
    upgrade(f"sqlite+aiosqlite:///{db}")
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO audit_log (at, action) VALUES ('2026-01-01', 'x')"))
        with engine.begin() as conn, pytest.raises(DatabaseError, match="append-only"):
            conn.execute(text("DELETE FROM audit_log"))
    finally:
        engine.dispose()


# ── per-server routing ────────────────────────────────────────────────────


async def _two_servers(sessions, *, other_valuations: bool = False) -> None:
    async with sessions() as session, session.begin():
        for gid, channel in ((HOME, "9001"), (OTHER, "9002")):
            row = await decide(session, gid, GuildStatus.APPROVED, actor_id=None)
            row.alert_channel_id = channel
        (await session.get(Guild, HOME)).show_valuations = True
        (await session.get(Guild, OTHER)).show_valuations = other_valuations


async def _back_in_stock(sessions) -> None:
    async with sessions() as session, session.begin():
        ingestor = Ingestor(session)
        await ingestor.ingest([make_snapshot()], observed_at=T0)
        await ingestor.ingest(
            [make_snapshot(status=StockStatus.OUT_OF_STOCK)], observed_at=T0 + timedelta(minutes=1)
        )
        await ingestor.ingest([make_snapshot()], observed_at=T0 + timedelta(minutes=2))
        listing_id = await session.scalar(select(Listing.id))
        await add_watches(session, "home-user", [listing_id], None, HOME)
        await add_watches(session, "other-user", [listing_id], None, OTHER)
        await add_watches(session, "legacy-user", [listing_id], None, None)
        await set_subscribed(session, "other-sub", True, OTHER)


async def test_pings_stay_in_the_server_where_they_were_set(sessions):
    await _two_servers(sessions)
    await _back_in_stock(sessions)
    async with sessions() as session:
        events = await load_events(session, pending_only=True, limit=10)
        configs = {c.guild_id: c for c in await approved_configs(session, discord())}
        router = AlertRouter()
        home_plans = await router.plan(session, events, configs[HOME], home_guild_id=HOME)
        other_plans = await router.plan(session, events, configs[OTHER], home_guild_id=HOME)
    home_back = next(p for p in home_plans if p.event.kind is EventKind.BACK_IN_STOCK)
    other_back = next(p for p in other_plans if p.event.kind is EventKind.BACK_IN_STOCK)
    assert home_back.mention_user_ids == ["home-user", "legacy-user"]  # legacy → home server
    assert other_back.mention_user_ids == ["other-sub", "other-user"]
    assert (home_back.channel_id, other_back.channel_id) == (9001, 9002)


async def test_valuations_hidden_where_not_enabled(sessions):
    await _two_servers(sessions, other_valuations=False)
    await _back_in_stock(sessions)
    async with sessions() as session, session.begin():
        listing = await session.scalar(select(Listing))
        session.add(
            Valuation(variant_id=listing.variant_id, computed_at=T0, net_cents=5000, roi=0.3,
                      resale_cents=26000, basis="sold", confidence="high")
        )  # fmt: skip
        await session.flush()
        from tracker.db.models import Event

        session.add(
            Event(listing_id=listing.id, kind="deal", occurred_at=T0 + timedelta(minutes=3))
        )
    async with sessions() as session:
        events = await load_events(session, pending_only=True, limit=10)
        other = await config_for(session, discord(), OTHER)
        plans = await AlertRouter().plan(session, events, other, home_guild_id=HOME)
    deal = next(p for p in plans if p.event.kind is EventKind.DEAL)
    assert deal.suppressed == "valuations hidden in this server"
    back = next(p for p in plans if p.event.kind is EventKind.BACK_IN_STOCK)
    assert back.event.net_cents is None
    assert back.event.profit_text is None


class FakeChannel:
    def __init__(self, channel_id: int, guild_id: int) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class FakeClient:
    def __init__(self, channels: dict[int, FakeChannel]) -> None:
        self.channels = channels

    async def wait_until_ready(self) -> None:
        return None

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        raise AssertionError("not expected")


async def test_sink_delivers_to_each_server_and_refuses_foreign_channels(sessions):
    await _two_servers(sessions)
    async with sessions() as session, session.begin():
        # OTHER's admin points alerts at a channel that belongs to HOME.
        (await session.get(Guild, OTHER)).alert_channel_id = "9001"
    await _back_in_stock(sessions)
    channels = {9001: FakeChannel(9001, int(HOME))}
    sink = DiscordSink(FakeClient(channels), sessions, AlertRouter(), discord())
    async with sessions() as session:
        await sink.publish(await load_events(session, pending_only=True, limit=10))
    contents = [m["content"] for m in channels[9001].sent]
    # Only HOME's own alert lands; OTHER's plan targeting HOME's channel is refused.
    assert sum("Back in stock" in c for c in contents) == 1
    assert all("other-user" not in c for c in contents)


# ── privacy ───────────────────────────────────────────────────────────────


async def test_forgetme_removes_member_data(sessions):
    await _two_servers(sessions)
    await _back_in_stock(sessions)
    async with sessions() as session, session.begin():
        listing = await session.scalar(select(Listing))
        session.add(
            MarketComp(variant_id=listing.variant_id, source="manual", external_id="m1",
                       title="t", price_cents=1, observed_at=T0, created_by="other-user")
        )  # fmt: skip
    async with sessions() as session, session.begin():
        counts = await forget_user(session, "other-user")
    assert counts == {"watches": 1, "comps_unlinked": 1}
    async with sessions() as session:
        users = {w.discord_user_id for w in await session.scalars(select(Watch))}
        assert "other-user" not in users
        assert await session.scalar(select(MarketComp.created_by)) is None


# ── command gate ──────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, content: str, **_: Any) -> None:
        self.messages.append(content)


def _interaction(command: str, guild_id: int | None, user_id: int, *, manage: bool = False):
    return SimpleNamespace(
        command=SimpleNamespace(qualified_name=command),
        guild_id=guild_id,
        user=SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(manage_guild=manage),
            roles=[],
        ),
        response=FakeResponse(),
        extras={},
    )


async def test_interaction_check_gates_commands(sessions):
    from tracker.bot.commands import TrackerCommands

    await _two_servers(sessions)
    ctx = SimpleNamespace(sessions=sessions, settings=SimpleNamespace(discord=discord()))
    cog = TrackerCommands(SimpleNamespace(ctx=ctx, owner_ids={"1"}))  # type: ignore[arg-type]

    member = _interaction("check", int(HOME), 50)
    assert not await cog.interaction_check(member)
    assert "server admins" in member.response.messages[0]

    admin = _interaction("check", int(HOME), 51, manage=True)
    assert await cog.interaction_check(admin)
    assert admin.extras["role"] is Role.SERVER_ADMIN

    stranger = _interaction("watch", 999, 1)  # even the owner can't use an unapproved server
    assert not await cog.interaction_check(stranger)
    assert "isn't approved" in stranger.response.messages[0]


# ── allowlist enforcement ─────────────────────────────────────────────────


async def test_allowlist_enforcement(sessions, monkeypatch: pytest.MonkeyPatch):
    from tracker.bot.client import TrackerBot

    settings = SimpleNamespace(discord=discord(), cleanup=None)
    bot = TrackerBot(SimpleNamespace(settings=settings, sessions=sessions))  # type: ignore[arg-type]
    left: list[str] = []
    asked: list[str] = []

    async def fake_leave(guild, reason):
        left.append(f"{guild.id}:{reason}")

    async def fake_request(guild):
        asked.append(str(guild.id))

    monkeypatch.setattr(bot, "_leave", fake_leave)
    monkeypatch.setattr(bot, "_request_approval", fake_request)

    await _two_servers(sessions)
    async with sessions() as session, session.begin():
        await decide(session, "444", GuildStatus.BLOCKED, actor_id="1")

    await bot.enforce_allowlist(SimpleNamespace(id=int(HOME), name="Home"))
    await bot.enforce_allowlist(SimpleNamespace(id=444, name="Blocked"))
    stranger = SimpleNamespace(id=555, name="Stranger")
    await bot.enforce_allowlist(stranger)
    assert left == ["444:blocked"]
    assert asked == ["555"]

    # Grace period runs out without approval → leave.
    bot._pending_leaves.pop(555).cancel()
    await bot._leave_if_unapproved(stranger, 0)
    assert left[-1] == "555:not approved in time"
    await asyncio.sleep(0)


# ── secret redaction ──────────────────────────────────────────────────────


def test_redactor_masks_configured_and_shaped_secrets():
    redact = Redactor(["PRD-0000fake0000-secret", "short"])  # gitleaks:allow
    token = ".".join(  # fake token-shaped value, built at runtime
        ["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GaBcDe", "abcdefghijklmnopqrstuvwxyz0123456"]
    ) (fake, test-only)
    line = (
        f"cert PRD-0000fake0000-secret token {token} Authorization: Bearer abc.def.ghi123 "
        "url https://x/?client_secret=hunter22&ok=1 password: swordfish short"
    )
    out = redact(line)
    assert "PRD-0000fake0000-secret" not in out
    assert token not in out
    assert "abc.def.ghi123" not in out
    assert "hunter22" not in out
    assert "swordfish" not in out
    assert "short" in out  # too short to be treated as a secret
    assert out.count(MASK) >= 5


def test_logging_redacts_even_tracebacks(capsys: pytest.CaptureFixture[str]):
    configure_logging("INFO", secrets=["SuperSecretValue123"])
    log = logging.getLogger("tracker.test")
    try:
        raise RuntimeError("failed with SuperSecretValue123")
    except RuntimeError:
        log.exception("boom SuperSecretValue123")
    err = capsys.readouterr().err
    assert "SuperSecretValue123" not in err
    assert "boom ***" in err
    assert "failed with ***" in err
