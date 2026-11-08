"""Owner panel: TOTP login, sessions, request guards, audit trail, and data endpoints."""

from __future__ import annotations

import argparse
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import make_snapshot
from tracker.db.models import AuditLog, EbayStats, Guild, Variant
from tracker.domain import StockStatus
from tracker.guilds import GuildStatus
from tracker.ingest import Ingestor
from tracker.market.service import MarketService
from tracker.panel.api import PanelDeps, create_app
from tracker.panel.auth import (
    MAX_FAILS_PER_CLIENT,
    SessionStore,
    TotpVerifier,
    new_totp_secret,
    provisioning_uri,
    totp_at,
)
from tracker.panel.server import PanelServer
from tracker.panel.setup import cmd_panel_setup, set_env_value
from tracker.settings import MarketSettings, PanelSettings, Settings

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # public demo key  # gitleaks:allow
BASE = "http://127.0.0.1:8765"
WRITE = {"X-FlipScout": "1"}
NOW = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now

    def code(self, offset_steps: int = 0) -> str:
        return totp_at(SECRET, int(self.now // 30) + offset_steps)


def panel_settings(**overrides: object) -> Settings:
    settings = Settings(env_file=None)
    values: dict[str, object] = {"totp_secret": SECRET, "secure_cookies": False}
    values.update(overrides)
    settings.panel = PanelSettings(_env_file=None, **values)  # type: ignore[call-arg]
    return settings


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_client(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    settings: Settings | None = None,
    **deps,
) -> httpx.AsyncClient:
    app = create_app(
        PanelDeps(
            settings=settings or panel_settings(),
            sessions=sessions,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            clock=clock,
            **deps,
        )
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
async def client(sessions, clock):
    async with make_client(sessions, clock) as c:
        yield c


async def login(client: httpx.AsyncClient, clock: Clock) -> None:
    response = await client.post("/api/session", json={"code": clock.code()}, headers=WRITE)
    assert response.status_code == 200, response.text


async def audit_actions(sessions) -> list[str]:
    async with sessions() as session:
        return list(await session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


# ── TOTP and sessions ────────────────────────────────────────────────────


def test_totp_matches_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key  # gitleaks:allow
    assert totp_at(secret, 59 // 30) == "287082"  # RFC 6238 T=59 → 94287082
    assert totp_at(secret, 1111111109 // 30) == "081804"


def test_totp_allows_drift_but_never_replay(clock):
    verifier = TotpVerifier(SECRET, clock=clock)
    assert verifier.verify(clock.code(-1))  # 30s of clock drift is fine
    assert not verifier.verify(clock.code(-1))  # but each code works once
    assert verifier.verify(clock.code())
    assert not verifier.verify(clock.code(-1))  # and older codes are dead after a newer one
    assert not verifier.verify(clock.code(2))  # too far ahead
    assert not verifier.verify("12345")
    assert not verifier.verify("abcdef")


def test_new_secret_and_uri():
    secret = new_totp_secret()
    assert len(secret) == 32
    TotpVerifier(secret)
    uri = provisioning_uri(secret)
    assert uri.startswith("otpauth://totp/FlipScout%3Aowner?secret=")
    assert "issuer=FlipScout" in uri


def test_sessions_expire_and_store_only_hashes(clock):
    store = SessionStore(60, clock=clock)
    token, _ = store.create("127.0.0.1")
    assert store.get(token) is not None
    assert token not in store._sessions  # keyed by SHA-256, not the cookie itself
    clock.now += 61
    assert store.get(token) is None


def test_panel_refuses_non_loopback_bind():
    with pytest.raises(ValidationError):
        PanelSettings(_env_file=None, host="0.0.0.0")  # type: ignore[call-arg]


# ── login flow ───────────────────────────────────────────────────────────


async def test_everything_needs_a_login(client):
    for path in ("/api/overview", "/api/market", "/api/events", "/api/servers", "/api/audit"):
        assert (await client.get(path)).status_code == 401, path
    assert (await client.get("/api/session")).json()["authenticated"] is False


async def test_login_logout_and_audit(client, clock, sessions):
    await login(client, clock)
    assert (await client.get("/api/session")).json()["authenticated"] is True
    assert (await client.get("/api/overview")).status_code == 200
    assert (await client.delete("/api/session", headers=WRITE)).status_code == 200
    assert (await client.get("/api/overview")).status_code == 401
    assert await audit_actions(sessions) == ["panel.login", "panel.logout"]


async def test_wrong_codes_lock_out_even_the_right_one(client, clock, sessions):
    for _ in range(MAX_FAILS_PER_CLIENT):
        response = await client.post("/api/session", json={"code": "000000"}, headers=WRITE)
        assert response.status_code == 401
    response = await client.post("/api/session", json={"code": clock.code()}, headers=WRITE)
    assert response.status_code == 429
    assert (await audit_actions(sessions)).count("panel.login_failed") == MAX_FAILS_PER_CLIENT
    clock.now += 15 * 60 + 1  # the window passes
    await login(client, clock)


async def test_revoke_all_ends_every_session(sessions, clock):
    # One app (one session store), two browsers.
    settings = panel_settings()
    app = create_app(PanelDeps(settings, sessions, datetime.now(UTC), clock=clock))
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url=BASE) as a,
        httpx.AsyncClient(transport=transport, base_url=BASE) as b,
    ):
        await login(a, clock)
        clock.now += 30
        await login(b, clock)
        assert (await a.post("/api/session/revoke-all", headers=WRITE)).status_code == 200
        assert (await b.get("/api/overview")).status_code == 401


# ── request guards ───────────────────────────────────────────────────────


async def test_unknown_host_is_refused(client):
    response = await client.get("/api/session", headers={"Host": "evil.example:8765"})
    assert response.status_code == 421  # DNS rebinding can't reach the API


async def test_writes_need_panel_header_and_same_origin(client, clock):
    response = await client.post("/api/session", json={"code": clock.code()})
    assert response.status_code == 403
    response = await client.post(
        "/api/session",
        json={"code": clock.code()},
        headers={**WRITE, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    response = await client.post(
        "/api/session", json={"code": clock.code()}, headers={**WRITE, "Origin": BASE}
    )
    assert response.status_code == 200


async def test_tailscale_login_allowlist(sessions, clock):
    settings = panel_settings(
        tailscale_logins="me@example.com", allowed_hosts="flipscout.tail1234.ts.net"
    )
    async with make_client(sessions, clock, settings) as c:
        tailnet = {"Host": "flipscout.tail1234.ts.net"}
        intruder = {**tailnet, "Tailscale-User-Login": "someone@else.com"}
        assert (await c.get("/api/session", headers=intruder)).status_code == 403
        me = {**tailnet, "Tailscale-User-Login": "Me@Example.com"}
        assert (await c.get("/api/session", headers=me)).status_code == 200


async def test_security_headers(client):
    response = await client.get("/")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    api = await client.get("/api/session")
    assert api.headers["cache-control"] == "no-store"
    assert (await client.get("/static/app.js")).status_code == 200
    assert (await client.get("/docs")).status_code == 404  # no API explorer


async def test_secure_cookie_attributes(sessions, clock):
    async with make_client(sessions, clock, panel_settings(secure_cookies=True)) as c:
        response = await c.post("/api/session", json={"code": clock.code()}, headers=WRITE)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-flipscout=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()
    assert "domain" not in cookie.lower()


# ── servers ──────────────────────────────────────────────────────────────


async def test_server_decision_without_bot_updates_db_and_audits(client, clock, sessions):
    await login(client, clock)
    response = await client.post(
        "/api/servers/123456789/decision", json={"status": "approved"}, headers=WRITE
    )
    assert response.status_code == 200
    async with sessions() as session:
        guild = await session.get(Guild, "123456789")
        assert guild.status == "approved"
        entry = await session.scalar(select(AuditLog).where(AuditLog.action == "guild.approved"))
        assert entry.actor_name.startswith("owner (panel")
    listed = (await client.get("/api/servers")).json()["items"]
    assert [g["guild_id"] for g in listed] == ["123456789"]
    bad = await client.post("/api/servers/abc/decision", json={"status": "approved"}, headers=WRITE)
    assert bad.status_code == 422


async def test_server_decision_goes_through_the_bot(sessions, clock):
    calls = []

    async def decide(guild_id, status, actor_id, actor_name):
        calls.append((guild_id, status))

    async with make_client(sessions, clock, decide_guild=decide) as c:
        await login(c, clock)
        response = await c.post(
            "/api/servers/42/decision", json={"status": "blocked"}, headers=WRITE
        )
    assert response.status_code == 200
    assert calls == [("42", GuildStatus.BLOCKED)]


# ── data ─────────────────────────────────────────────────────────────────


class Dispatcher:
    async def dispatch(self) -> int:
        return 0


async def test_market_product_and_events(client, clock, sessions):
    at = datetime(2026, 11, 7, 12, tzinfo=UTC)
    async with sessions() as session, session.begin():
        await Ingestor(session).ingest(
            [
                make_snapshot(
                    external_id="CAM-US",
                    sku="CAM",
                    product_key="ubiquiti:cam",
                    price_cents=12900,
                    status=StockStatus.OUT_OF_STOCK,
                )
            ],
            observed_at=at,
        )
        variant_id = await session.scalar(select(Variant.id))
        session.add(
            EbayStats(variant_id=variant_id, fetched_at=at, active_count=6, active_listings=6,
                      active_p25_cents=22000, active_median_cents=23000)
        )  # fmt: skip
    settings = MarketSettings(_env_file=None)  # type: ignore[call-arg]
    await MarketService(sessions, None, settings, Dispatcher(), clock=lambda: at).revalue(  # type: ignore[arg-type]
        [variant_id]
    )
    await login(client, clock)

    sold_out = (await client.get("/api/market?view=sold_out")).json()["items"]
    assert [r["sku"] for r in sold_out] == ["CAM"]
    assert sold_out[0]["valuation"]["basis"] == "active"
    assert sold_out[0]["valuation"]["resale_cents"] == 22000
    assert (await client.get("/api/market?view=capped")).json()["items"] == []
    assert (await client.get("/api/market?view=sold_out&q=nomatch")).json()["items"] == []

    by_id = (await client.get(f"/api/products/{variant_id}")).json()
    by_sku = (await client.get("/api/products/cam")).json()
    assert by_id["sku"] == by_sku["sku"] == "CAM"
    assert by_id["listings"][0]["status"] == "out_of_stock"
    assert by_id["valuation"]["net_cents"] is not None
    assert (await client.get("/api/products/999999")).status_code == 404

    found = (await client.get("/api/search?q=ca")).json()["items"]
    assert found[0]["variant_id"] == variant_id
    overview = (await client.get("/api/overview")).json()
    assert overview["valuations"]["active"] == 1
    assert overview["sources"]["ebay"] in (True, False)
    assert "secret" not in str(overview).lower()
    assert (await client.get("/api/events")).status_code == 200
    assert (await client.get("/api/events?kind=deal")).json()["items"] == []
    audit = (await client.get("/api/audit")).json()["items"]
    assert audit[0]["action"] == "panel.login"


# ── server + CLI ─────────────────────────────────────────────────────────


def test_busy_port_disables_panel_not_tracker(sessions, clock):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        settings = panel_settings(port=port)
        app = create_app(PanelDeps(settings, sessions, datetime.now(UTC), clock=clock))
        assert PanelServer(app, settings.panel).bind() is False


def test_set_env_value_replaces_or_appends(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("A=1\nPANEL_TOTP_SECRET=old\nB=2\n", encoding="utf-8")
    set_env_value(env, "PANEL_TOTP_SECRET", "new")
    assert env.read_text(encoding="utf-8") == "A=1\nPANEL_TOTP_SECRET=new\nB=2\n"
    other = tmp_path / "other.env"
    other.write_text("A=1", encoding="utf-8")
    set_env_value(other, "PANEL_TOTP_SECRET", "x")
    assert other.read_text(encoding="utf-8").splitlines()[-1] == "PANEL_TOTP_SECRET=x"


async def test_setup_cli_writes_secret_and_refuses_overwrite(tmp_path: Path, capsys):
    env = tmp_path / ".env"
    env.write_text("KEEP=me\n", encoding="utf-8")
    args = argparse.Namespace(env_file=env, rotate=False, yes=True)
    settings = Settings(env_file=None)
    settings.panel = PanelSettings(_env_file=None)  # type: ignore[call-arg]
    assert await cmd_panel_setup(settings, args) == 0
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "KEEP=me" in lines
    [entry] = [line for line in lines if line.startswith("PANEL_TOTP_SECRET=")]
    TotpVerifier(entry.split("=", 1)[1])

    settings.panel = PanelSettings(_env_file=None, totp_secret="X" * 32)  # type: ignore[call-arg]
    assert await cmd_panel_setup(settings, args) == 0
    assert "already set up" in capsys.readouterr().out
    assert env.read_text(encoding="utf-8").splitlines() == lines  # untouched
