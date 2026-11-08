"""Single-instance guards for the long-running tracker.

Two FlipScout processes on one database fight: each revalues, flips `deal_active`, and
re-alerts what the other just cleared. On 2026-11-07 a forgotten instance with stale code
sent 168 bogus deal alerts that way. Two guards prevent a repeat:

1. **Instance lock:** an OS file lock beside the database, held for the process lifetime.
   A second `tracker run` on the same database refuses to start and names the holder.
   The OS drops the lock when the process dies, so a crash never leaves a stale lock.
2. **Schema guard:** a running instance checks the database's migration revision. If newer
   code migrated it (the stale-instance case), the old instance stops itself instead of
   applying outdated rules.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from tracker import __version__
from tracker.db.migrate import alembic_config

logger = logging.getLogger(__name__)

# Windows locks a byte range; lock one far past the holder info so others can still read it.
_WIN_LOCK_OFFSET = 1 << 30


class AlreadyRunningError(RuntimeError):
    """Another tracker instance holds the lock for this database."""

    def __init__(self, lock_path: Path, holder: dict[str, object] | None) -> None:
        self.lock_path = lock_path
        self.holder = holder or {}
        who = (
            f"PID {self.holder.get('pid')} on {self.holder.get('host')}, "
            f"started {self.holder.get('started_at')} (v{self.holder.get('version')})"
            if self.holder
            else "an unknown process"
        )
        super().__init__(
            f"Another FlipScout instance is already running on this database: {who}. "
            f"Stop it first (lock: {lock_path})."
        )


def lock_path_for(database_url: str, data_dir: Path) -> Path:
    """`<db file>.lock` for SQLite files; otherwise a lock in the data directory."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        db = Path(url.database).resolve()
        return db.with_name(db.name + ".lock")
    return data_dir.resolve() / "tracker.lock"


def _try_lock(handle: IO[str]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[str]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def read_holder(lock_path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(lock_path.read_text("utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class InstanceLock:
    """Exclusive, crash-safe lock for one tracker per database (context manager)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if not _try_lock(handle):
            handle.close()
            raise AlreadyRunningError(self.path, read_holder(self.path))
        holder = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "version": __version__,
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(holder))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            handle.truncate()  # Holder info is stale once we let go.
            handle.flush()
        except OSError:
            pass
        _unlock(handle)
        handle.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


# ── schema guard ─────────────────────────────────────────────────────────


def code_schema_head(database_url: str) -> str | None:
    """The newest migration this code knows about."""
    return ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()


async def database_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.scalar(text("SELECT version_num FROM alembic_version"))


@dataclass(frozen=True, slots=True)
class SchemaGuard:
    """Detects a database migrated past this code (a newer instance took over)."""

    engine: AsyncEngine
    expected: str | None

    async def check(self) -> bool:
        """True while the database still matches this code."""
        current = await database_revision(self.engine)
        if current == self.expected:
            return True
        logger.critical(
            "Database schema is %s but this code expects %s: a newer FlipScout migrated it. "
            "Stopping this outdated instance.",
            current,
            self.expected,
        )
        return False
