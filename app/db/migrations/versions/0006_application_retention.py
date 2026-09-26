"""retention for deleted applications

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_applications_purge_after", ["purge_after"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.drop_index("ix_applications_purge_after")
        batch_op.drop_column("purge_after")
        batch_op.drop_column("deleted_at")
