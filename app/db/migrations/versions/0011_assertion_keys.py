"""per-application assertion keys

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-04 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assertion_keys",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=40), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_assertion_keys_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assertion_keys")),
        sa.UniqueConstraint("key_id", name=op.f("uq_assertion_keys_key_id")),
    )
    with op.batch_alter_table("assertion_keys", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_assertion_keys_application_id"), ["application_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("assertion_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_assertion_keys_application_id"))
    op.drop_table("assertion_keys")
