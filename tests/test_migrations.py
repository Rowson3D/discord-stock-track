from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from tracker.db.migrate import upgrade
from tracker.db.models import Base


def test_migrations_match_models(tmp_path: Path):
    """Fails when a model changes without a matching migration."""
    db_file = (tmp_path / "migrated.db").as_posix()
    upgrade(f"sqlite+aiosqlite:///{db_file}")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
            tables = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()

    assert diff == []
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_upgrade_preserves_existing_data(tmp_path: Path):
    """Migrations must work on a database that already has rows, not just an empty one
    (a NOT NULL column without a server default fails only when rows exist)."""
    db = (tmp_path / "populated.db").as_posix()
    upgrade(f"sqlite+aiosqlite:///{db}", "0007")
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO products (id, key, brand, name, created_at, updated_at) "
                "VALUES (1, 'ubiquiti:u7-pro', 'Ubiquiti', 'U7 Pro', '2026-01-01', '2026-01-01')"
            )
            conn.exec_driver_sql(
                "INSERT INTO variants (id, product_id, sku, sku_key, pack_size, created_at) "
                "VALUES (1, 1, 'U7-Pro', 'u7-pro', 1, '2026-01-01')"
            )
            conn.exec_driver_sql(
                "INSERT INTO ebay_stats (variant_id, fetched_at, sold_count, sold_30d, "
                "active_count, matched, excluded) VALUES (1, '2026-01-01', 0, 0, 5, 5, 1)"
            )
    finally:
        engine.dispose()

    upgrade(f"sqlite+aiosqlite:///{db}")
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT active_count, active_listings FROM ebay_stats WHERE variant_id = 1"
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == (5, 0)
