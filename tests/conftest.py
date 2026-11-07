from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.db.models import Base
from tracker.db.session import create_engine, create_sessionmaker
from tracker.domain import ListingSnapshot, SourceKey, StockStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return read


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def session(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessions() as db_session:
        yield db_session


def make_snapshot(**overrides: object) -> ListingSnapshot:
    base = ListingSnapshot(
        source=SourceKey.UNIFI,
        region="us",
        external_id="U7-Pro-US",
        url="https://store.ui.com/us/en/products/u7-pro",
        product_key="ubiquiti:u7-pro",
        product_name="Access Point U7 Pro",
        brand="Ubiquiti",
        category="wifi-flagship",
        sku="U7-Pro",
        variant_title="Single Unit",
        pack_size=1,
        status=StockStatus.IN_STOCK,
        raw_status="Available",
        price_cents=18900,
        regular_price_cents=None,
        currency="USD",
        is_early_access=False,
        sold_out_at=None,
        restock_eta=None,
    )
    return dataclasses.replace(base, **overrides)


T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
