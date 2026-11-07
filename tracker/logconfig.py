"""Logging setup. Call `configure_logging()` from an entry point; importing has no side effects.

Every line that leaves the process passes through `Redactor`: configured secrets (bot token,
API secrets) and anything shaped like a credential are masked, including inside exception
tracebacks, since redaction runs on the fully formatted text.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MARKER = "_tracker_handler"
_NOISY_LOGGERS = ("discord", "discord.http", "httpx", "httpcore", "aiosqlite", "sqlalchemy.engine")
_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MASK = "***"

# Credential shapes masked even if they aren't configured secrets.
_PATTERNS = (
    # Discord bot tokens: base64 user id . timestamp . HMAC
    re.compile(r"\b[MNO][A-Za-z\d_-]{22,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}\b"),
    # HTTP auth headers and bearer tokens
    re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # key=value style secrets in URLs or messages
    re.compile(
        r"(?i)\b(token|secret|password|passwd|api[_-]?key|access_token|client_secret)"
        r"(\"?\s*[:=]\s*\"?)[^\s\"'&,]{4,}"
    ),
)


class Redactor:
    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        # Longest first so a secret that contains another is masked whole.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        text = _PATTERNS[0].sub(MASK, text)
        text = _PATTERNS[1].sub(lambda m: f"{m.group(1)} {MASK}", text)
        return _PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, inner: logging.Formatter, redact: Redactor) -> None:
        super().__init__()
        self._inner = inner
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        return self._redact(self._inner.format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    json_format: bool = False,
    secrets: Iterable[str | None] = (),
) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _MARKER, False)]:
        root.removeHandler(handler)
        handler.close()

    inner: logging.Formatter = JsonFormatter() if json_format else logging.Formatter(_TEXT_FORMAT)
    formatter = RedactingFormatter(inner, Redactor(secrets))
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        )

    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _MARKER, True)
        root.addHandler(handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
