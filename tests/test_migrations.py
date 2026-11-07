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
