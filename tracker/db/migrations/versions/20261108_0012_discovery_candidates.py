"""discovery candidates

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-08 05:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tracker.db.types

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("upc", sa.String(length=20), nullable=True),
        sa.Column("category", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="new", nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("retail_cents", sa.Integer(), nullable=True),
        sa.Column("regular_cents", sa.Integer(), nullable=True),
        sa.Column("retail_url", sa.String(length=500), nullable=True),
        sa.Column("retail_source", sa.String(length=32), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("heat", sa.Float(), server_default="0", nullable=False),
        sa.Column("signal_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_seen_at", tracker.db.types.UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", tracker.db.types.UTCDateTime(), nullable=False),
        sa.Column("checked_at", tracker.db.types.UTCDateTime(), nullable=True),
        sa.Column("next_check_at", tracker.db.types.UTCDateTime(), nullable=True),
        sa.Column("ebay_query", sa.String(length=200), nullable=True),
        sa.Column("ebay_sellers", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ebay_listings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ebay_p25_cents", sa.Integer(), nullable=True),
        sa.Column("ebay_median_cents", sa.Integer(), nullable=True),
        sa.Column("ebay_low_cents", sa.Integer(), nullable=True),
        sa.Column("resale_cents", sa.Integer(), nullable=True),
        sa.Column("net_cents", sa.Integer(), nullable=True),
        sa.Column("roi", sa.Float(), nullable=True),
        sa.Column("confidence", sa.String(length=8), server_default="none", nullable=False),
        sa.Column("capped", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("score", sa.Float(), server_default="0", nullable=False),
        sa.Column("trial_until", tracker.db.types.UTCDateTime(), nullable=True),
        sa.Column("decided_at", tracker.db.types.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidates")),
        sa.UniqueConstraint("key", name=op.f("uq_candidates_key")),
    )
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_candidates_next_check_at"), ["next_check_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_candidates_status"), ["status"], unique=False)

    op.create_table(
        "candidate_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("fetched_at", tracker.db.types.UTCDateTime(), nullable=False),
        sa.Column("sellers", sa.Integer(), nullable=False),
        sa.Column("listings", sa.Integer(), nullable=False),
        sa.Column("p25_cents", sa.Integer(), nullable=True),
        sa.Column("median_cents", sa.Integer(), nullable=True),
        sa.Column("retail_cents", sa.Integer(), nullable=True),
        sa.Column("net_cents", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_candidate_history_candidate_id_candidates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_history")),
        sa.UniqueConstraint(
            "candidate_id", "day", name=op.f("uq_candidate_history_candidate_id_day")
        ),
    )
    with op.batch_alter_table("candidate_history", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_candidate_history_candidate_id"), ["candidate_id"], unique=False
        )

    op.create_table(
        "candidate_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("times_seen", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_seen_at", tracker.db.types.UTCDateTime(), nullable=False),
        sa.Column("seen_at", tracker.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_candidate_signals_candidate_id_candidates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_signals")),
        sa.UniqueConstraint(
            "candidate_id",
            "source",
            "kind",
            name=op.f("uq_candidate_signals_candidate_id_source_kind"),
        ),
    )
    with op.batch_alter_table("candidate_signals", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_candidate_signals_candidate_id"), ["candidate_id"], unique=False
        )


def downgrade() -> None:
    op.drop_table("candidate_signals")
    op.drop_table("candidate_history")
    op.drop_table("candidates")
