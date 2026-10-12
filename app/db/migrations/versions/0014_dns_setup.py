"""dns zones and each domain's zone

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-12 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dns_zones",
        sa.Column("zone", sa.String(length=253), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("nameservers", sa.JSON(), nullable=True),
        sa.Column("domain_connect", sa.JSON(), nullable=True),
        sa.Column("template", sa.String(length=160), nullable=True),
        sa.Column("template_supported", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dns_zones")),
        sa.UniqueConstraint("zone", name=op.f("uq_dns_zones_zone")),
    )
    with op.batch_alter_table("domains", schema=None) as batch_op:
        batch_op.add_column(sa.Column("zone", sa.String(length=253), nullable=True))
        batch_op.create_index("ix_domains_zone", ["zone"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("domains", schema=None) as batch_op:
        batch_op.drop_index("ix_domains_zone")
        batch_op.drop_column("zone")
    op.drop_table("dns_zones")
