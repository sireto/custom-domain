"""per-application traffic counted at the edge

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-04 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_traffic",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("response_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_application_traffic_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("application_id", "day", name=op.f("pk_application_traffic")),
    )
    op.create_table(
        "edge_traffic_counters",
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("response_bytes", sa.BigInteger(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("hostname", name=op.f("pk_edge_traffic_counters")),
    )


def downgrade() -> None:
    op.drop_table("edge_traffic_counters")
    op.drop_table("application_traffic")
