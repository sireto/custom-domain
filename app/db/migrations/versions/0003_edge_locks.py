"""edge locks

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 16:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "edge_locks",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("holder", sa.String(length=128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_edge_locks")),
    )
    op.execute("INSERT INTO edge_locks (name) VALUES ('reconcile')")


def downgrade() -> None:
    op.drop_table("edge_locks")
