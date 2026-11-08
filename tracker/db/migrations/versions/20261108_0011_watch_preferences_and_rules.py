"""watch preferences and rules

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-08 03:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tracker.db.types

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("watches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("kinds", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("muted_until", tracker.db.types.UTCDateTime(), nullable=True))

    op.create_table(
        "watch_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", sa.String(length=32), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("query", sa.String(length=80), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("regions", sa.String(length=100), nullable=True),
        sa.Column("kinds", sa.String(length=200), nullable=True),
        sa.Column("max_price_cents", sa.Integer(), nullable=True),
        sa.Column("min_profit_cents", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("muted_until", tracker.db.types.UTCDateTime(), nullable=True),
        sa.Column("created_at", tracker.db.types.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_rules")),
    )
    with op.batch_alter_table("watch_rules", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_watch_rules_discord_user_id"), ["discord_user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_watch_rules_guild_id"), ["guild_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("watch_rules", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_watch_rules_guild_id"))
        batch_op.drop_index(batch_op.f("ix_watch_rules_discord_user_id"))
    op.drop_table("watch_rules")
    with op.batch_alter_table("watches", schema=None) as batch_op:
        batch_op.drop_column("muted_until")
        batch_op.drop_column("kinds")
