"""connect sessions: links to the setup page for one domain

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-13 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connect_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("app_name", sa.String(length=64), nullable=True),
        sa.Column("return_url", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_connect_sessions_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_connect_sessions_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connect_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_connect_sessions_token_hash")),
    )
    with op.batch_alter_table("connect_sessions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_connect_sessions_domain_created", ["domain_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_connect_sessions_application_created",
            ["application_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("connect_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_connect_sessions_application_created")
        batch_op.drop_index("ix_connect_sessions_domain_created")
    op.drop_table("connect_sessions")
