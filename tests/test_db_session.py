"""SQLite locking: concurrent writers wait their turn, and reads never wait for a writer."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select, text

from tracker.db.models import AuditLog


def entry(action: str) -> AuditLog:
    return AuditLog(action=action)


async def test_write_after_read_survives_another_writer(sessions):
    """B reads, A commits a write, then B writes: B must not fail with "database is locked".

    (A transaction only takes the write lock at its first write, so B's read is no snapshot.)
    """
    b_has_read = asyncio.Event()

    async def writer_a() -> None:
        await b_has_read.wait()
        async with sessions() as session, session.begin():
            session.add(entry("a"))

    async def writer_b() -> None:
        async with sessions() as session, session.begin():
            await session.scalar(select(func.count()).select_from(AuditLog))
            b_has_read.set()
            await asyncio.sleep(0.2)  # A tries to write meanwhile
            session.add(entry("b"))

    await asyncio.wait_for(asyncio.gather(writer_a(), writer_b()), timeout=20)
    async with sessions() as session:
        actions = sorted(await session.scalars(select(AuditLog.action)))
    assert actions == ["a", "b"]


async def test_reads_do_not_wait_for_a_writer(sessions):
    async with sessions() as writer, writer.begin():
        writer.add(entry("pending"))
        await writer.flush()  # holds the write lock with an uncommitted row
        async with sessions() as reader:
            count = await asyncio.wait_for(
                reader.scalar(select(func.count()).select_from(AuditLog)), timeout=2
            )
        assert count == 0  # the reader sees the last committed state, immediately


async def test_pragmas(sessions):
    async with sessions() as session:
        assert (await session.execute(text("PRAGMA busy_timeout"))).scalar() == 30_000
        assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
