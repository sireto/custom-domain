"""domain connect signer and each domain's connect start

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-12 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "domain_connect_signers",
        sa.Column("name", sa.String(length=16), nullable=False),
        sa.Column("signer", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=False),
        sa.Column("credential_prefix", sa.String(length=16), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_domain_connect_signers")),
    )
    with op.batch_alter_table("domains", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("connect_started_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("domains", schema=None) as batch_op:
        batch_op.drop_column("connect_started_at")
    op.drop_table("domain_connect_signers")
