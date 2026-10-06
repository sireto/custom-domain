"""origin host header mode

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("verified_origins", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "host_header",
                sa.Enum(
                    "customer",
                    "origin",
                    name="origin_host_header",
                    native_enum=False,
                    create_constraint=False,
                    length=32,
                ),
                server_default="customer",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "origin_host_header", "host_header IN ('customer', 'origin')"
        )


def downgrade() -> None:
    with op.batch_alter_table("verified_origins", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_verified_origins_origin_host_header"), type_="check"
        )
        batch_op.drop_column("host_header")
