"""Programmatic Alembic helpers so the app can migrate itself at startup."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from tracker.db.session import ensure_sqlite_dir

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    ensure_sqlite_dir(database_url)
    command.upgrade(alembic_config(database_url), revision)


async def upgrade_async(database_url: str, revision: str = "head") -> None:
    # Alembic's async env.py runs its own event loop, so it must run off this loop's thread.
    await asyncio.to_thread(upgrade, database_url, revision)
