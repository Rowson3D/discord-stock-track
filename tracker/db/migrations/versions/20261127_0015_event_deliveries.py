"""per-destination alert delivery, and indexes for the outbox and the drop detector

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-27 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from tracker.db.types import UTCDateTime

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_deliveries",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("sink", sa.String(length=32), nullable=False),
        sa.Column("delivered_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id", "sink"),
    )
    op.create_index("ix_events_notified_at", "events", ["notified_at"])
    op.create_index("ix_events_kind_occurred_at", "events", ["kind", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_events_kind_occurred_at", table_name="events")
    op.drop_index("ix_events_notified_at", table_name="events")
    op.drop_table("event_deliveries")
