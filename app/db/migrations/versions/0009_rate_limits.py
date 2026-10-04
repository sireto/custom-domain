"""per-application request limits at the edge

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-04 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("rate_limit_per_second", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.drop_column("rate_limit_per_second")
        batch_op.drop_column("rate_limit_per_minute")
