"""webhook subscriptions and deliveries

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("previous_secret", sa.String(length=128), nullable=True),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_webhook_subscriptions_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_subscriptions")),
    )
    with op.batch_alter_table("webhook_subscriptions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_webhook_subscriptions_application_id"), ["application_id"], unique=False
        )

    op.create_table(
        "webhook_deliveries",
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_webhook_deliveries_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            name=op.f("fk_webhook_deliveries_subscription_id_webhook_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
        sa.UniqueConstraint(
            "subscription_id",
            "event_id",
            name=op.f("uq_webhook_deliveries_subscription_id_event_id"),
        ),
    )
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webhook_deliveries_due", ["next_attempt_at", "delivered_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_webhook_deliveries_application_id"), ["application_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_deliveries_application_id"))
        batch_op.drop_index("ix_webhook_deliveries_due")
    op.drop_table("webhook_deliveries")
    with op.batch_alter_table("webhook_subscriptions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_subscriptions_application_id"))
    op.drop_table("webhook_subscriptions")
