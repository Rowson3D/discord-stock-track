"""subscription regions

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("watches") as batch:
        batch.add_column(sa.Column("regions", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("watches") as batch:
        batch.drop_column("regions")
