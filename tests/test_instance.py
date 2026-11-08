"""Single-instance guards: one tracker per database, and outdated instances stop themselves."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import text

from tracker.db.migrate import upgrade_async
from tracker.db.session import create_engine
from tracker.instance import (
    AlreadyRunningError,
    InstanceLock,
    SchemaGuard,
    code_schema_head,
    lock_path_for,
    read_holder,
)

HOLD_LOCK = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from tracker.instance import InstanceLock
    lock = InstanceLock(Path(sys.argv[1])).acquire()
    print(os.getpid(), flush=True)
    sys.stdin.readline()  # hold until the test says so (or closes the pipe)
    """
)


def test_lock_path_sits_beside_the_sqlite_file(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'x' / 'tracker.db').as_posix()}"
    assert lock_path_for(url, tmp_path) == (tmp_path / "x" / "tracker.db.lock").resolve()
    assert lock_path_for("postgresql+asyncpg://h/db", tmp_path) == tmp_path.resolve() / (
        "tracker.lock"
    )


def test_lock_records_holder_and_releases(tmp_path: Path):
    path = tmp_path / "tracker.db.lock"
    with InstanceLock(path):
        holder = read_holder(path)
        assert holder is not None
        assert holder["pid"] > 0
        assert {"host", "started_at", "version"} <= holder.keys()
    assert read_holder(path) is None  # cleared on release
    with InstanceLock(path):  # and it can be taken again
        pass


def _holder_process(path: Path) -> tuple[subprocess.Popen[str], int]:
    """A child process holding the lock, plus the interpreter's real PID.

    (On Windows a venv's python.exe is a launcher, so Popen.pid isn't the interpreter.)
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    return proc, int(proc.stdout.readline())


def test_second_process_is_refused_and_told_who_holds_it(tmp_path: Path):
    path = tmp_path / "tracker.db.lock"
    proc, pid = _holder_process(path)
    try:
        with pytest.raises(AlreadyRunningError) as caught:
            InstanceLock(path).acquire()
        assert caught.value.holder["pid"] == pid
        assert f"PID {pid}" in str(caught.value)
    finally:
        proc.communicate("\n", timeout=10)
    with InstanceLock(path):  # holder exited cleanly: free again
        pass


def test_crashed_holder_never_leaves_a_stale_lock(tmp_path: Path):
    path = tmp_path / "tracker.db.lock"
    proc, pid = _holder_process(path)
    os.kill(pid, signal.SIGTERM)  # hard kill of the interpreter itself: no cleanup runs
    proc.wait(timeout=10)  # (on Windows, the venv launcher exits with it)
    with InstanceLock(path):  # the OS released it
        pass


async def test_schema_guard_stops_outdated_instance(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'tracker.db').as_posix()}"
    await upgrade_async(url)
    engine = create_engine(url)
    try:
        guard = SchemaGuard(engine, code_schema_head(url))
        assert await guard.check()
        async with engine.begin() as conn:  # a newer FlipScout migrates past us
            await conn.execute(text("UPDATE alembic_version SET version_num = 'future_0099'"))
        assert not await guard.check()
    finally:
        await engine.dispose()
