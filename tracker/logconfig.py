"""Logging setup. Call `configure_logging()` from an entry point; importing has no side effects."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MARKER = "_tracker_handler"
_NOISY_LOGGERS = ("discord", "discord.http", "httpx", "httpcore", "aiosqlite", "sqlalchemy.engine")
_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


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
    level: str = "INFO", log_file: Path | None = None, json_format: bool = False
) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _MARKER, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if json_format else logging.Formatter(_TEXT_FORMAT)
    )
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
