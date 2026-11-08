"""Owner login for the control panel: TOTP codes, server-side sessions, and lockouts.

- **TOTP** (RFC 6238, SHA-1, 6 digits, 30 s): works with any authenticator app. Codes are
  accepted one step either side for clock drift, and each step only once (no replay).
- **Sessions** live in memory and are keyed by a hash of the cookie token, so a memory dump
  or log line never yields a usable cookie. They expire after `session_hours` and end when
  the tracker restarts.
- **Lockout:** 5 failed codes per client or 20 overall in 15 minutes blocks logins for the
  rest of that window. A 6-digit code then can't be brute-forced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
FAIL_WINDOW_SECONDS = 15 * 60
MAX_FAILS_PER_CLIENT = 5
MAX_FAILS_TOTAL = 20


def new_totp_secret() -> str:
    """160 random bits, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


MIN_SECRET_BYTES = 10  # 80 bits: RFC 4226's minimum (setup generates 160).


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def secret_problem(secret: str | None) -> str | None:
    """Why `secret` can't be an authenticator key, or None if it can."""
    if not secret:
        return "not set"
    try:
        key = _key(secret)
    except ValueError:
        return "not a valid authenticator key (base32: letters A-Z and digits 2-7)"
    if len(key) < MIN_SECRET_BYTES:
        return f"too short ({len(key) * 8} bits; at least {MIN_SECRET_BYTES * 8} needed)"
    return None


def totp_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def provisioning_uri(secret: str, account: str = "owner", issuer: str = "FlipScout") -> str:
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


class TotpVerifier:
    def __init__(self, secret: str, clock: Callable[[], float] = time.time) -> None:
        problem = secret_problem(secret)
        if problem:  # Fail fast; never run with a weak or broken key.
            raise ValueError(f"PANEL_TOTP_SECRET is {problem}")
        self._secret = secret
        self._clock = clock
        self._last_step = -1

    def verify(self, code: str) -> bool:
        code = code.strip().replace(" ", "")
        if len(code) != DIGITS or not code.isdigit():
            return False
        now = int(self._clock() // STEP_SECONDS)
        for step in (now - 1, now, now + 1):
            if step > self._last_step and hmac.compare_digest(totp_at(self._secret, step), code):
                self._last_step = step  # Each code works once.
                return True
        return False


@dataclass(slots=True)
class Session:
    created: float
    expires: float
    client: str


class SessionStore:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, client: str) -> tuple[str, Session]:
        self._purge()
        token = secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(created=now, expires=now + self._ttl, client=client)
        self._sessions[self._digest(token)] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(self._digest(token))
        if session is None:
            return None
        if session.expires <= self._clock():
            self._sessions.pop(self._digest(token), None)
            return None
        return session

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._digest(token), None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, s in self._sessions.items() if s.expires <= now]:
            del self._sessions[key]

    def __len__(self) -> int:
        self._purge()
        return len(self._sessions)


class LoginLimiter:
    """Failed-login throttle, per client and overall."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._per_client: dict[str, deque[float]] = {}
        self._total: deque[float] = deque()

    def _trim(self, times: deque[float]) -> deque[float]:
        cutoff = self._clock() - FAIL_WINDOW_SECONDS
        while times and times[0] <= cutoff:
            times.popleft()
        return times

    def blocked(self, client: str) -> bool:
        mine = self._trim(self._per_client.get(client, deque()))
        return len(mine) >= MAX_FAILS_PER_CLIENT or len(self._trim(self._total)) >= MAX_FAILS_TOTAL

    def failed(self, client: str) -> None:
        now = self._clock()
        self._per_client.setdefault(client, deque()).append(now)
        self._total.append(now)

    def succeeded(self, client: str) -> None:
        self._per_client.pop(client, None)
