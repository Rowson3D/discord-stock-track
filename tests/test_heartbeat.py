"""The heartbeat file Docker's health check reads."""

from __future__ import annotations

import os
import time
from pathlib import Path

from tracker.heartbeat import beat, fresh


async def test_beats_only_while_connected(sessions, tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert not await beat(sessions, lambda: False, path)
    assert not path.exists()
    assert not fresh(path)
    assert await beat(sessions, lambda: True, path)
    assert fresh(path)
    assert await beat(sessions, None, path)  # headless: no Discord to wait for


def test_an_old_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.touch()
    old = time.time() - 120
    os.utime(path, (old, old))
    assert not fresh(path)
