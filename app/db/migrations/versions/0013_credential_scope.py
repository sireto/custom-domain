"""credential scope

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-12 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing credentials take the server default, "full": they keep working
    # exactly as before. The previous release does not read the column.
    with op.batch_alter_table("api_credentials", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope",
                sa.Enum(
                    "full",
                    "read",
                    name="credential_scope",
                    native_enum=False,
                    create_constraint=False,
                    length=32,
                ),
                server_default="full",
                nullable=False,
            )
        )
        batch_op.create_check_constraint("credential_scope", "scope IN ('full', 'read')")


def downgrade() -> None:
    with op.batch_alter_table("api_credentials", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("ck_api_credentials_credential_scope"), type_="check")
        batch_op.drop_column("scope")
