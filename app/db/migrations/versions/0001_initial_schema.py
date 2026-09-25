"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-25 15:04:15.713844

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "suspended",
                name="application_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("cname_target", sa.String(length=253), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name=op.f("ck_applications_application_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
        sa.UniqueConstraint("slug", name=op.f("uq_applications_slug")),
    )
    op.create_table(
        "api_credentials",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_api_credentials_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_credentials")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_credentials_key_hash")),
    )
    with op.batch_alter_table("api_credentials", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_api_credentials_application_id"), ["application_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_api_credentials_key_prefix"), ["key_prefix"], unique=False
        )

    op.create_table(
        "domains",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_dns",
                "provisioning",
                "ready",
                "attention_required",
                "suspended",
                "deleting",
                name="domain_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_dns', 'provisioning', 'ready', 'attention_required', "
            "'suspended', 'deleting')",
            name=op.f("ck_domains_domain_status"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_domains_application_id_applications"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domains")),
    )
    with op.batch_alter_table("domains", schema=None) as batch_op:
        batch_op.create_index(
            "ix_domains_application_reference", ["application_id", "reference"], unique=False
        )
        batch_op.create_index(
            "ix_domains_application_status", ["application_id", "status"], unique=False
        )
        batch_op.create_index("ix_domains_purge_after", ["purge_after"], unique=False)
        batch_op.create_index(
            "ux_domains_hostname_live",
            ["hostname"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
            sqlite_where=sa.text("deleted_at IS NULL"),
        )

    op.create_table(
        "verified_origins",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "verified",
                "failed",
                "retired",
                name="origin_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("verification_token", sa.String(length=128), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scheme IN ('https', 'http')", name=op.f("ck_verified_origins_scheme")),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'failed', 'retired')",
            name=op.f("ck_verified_origins_origin_status"),
        ),
        sa.CheckConstraint(
            "port > 0 AND port < 65536", name=op.f("ck_verified_origins_port_range")
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_verified_origins_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verified_origins")),
        sa.UniqueConstraint(
            "application_id",
            "scheme",
            "host",
            "port",
            name=op.f("uq_verified_origins_application_id_scheme_host_port"),
        ),
    )
    with op.batch_alter_table("verified_origins", schema=None) as batch_op:
        batch_op.create_index(
            "ux_verified_origins_one_active",
            ["application_id"],
            unique=True,
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active"),
        )

    op.create_table(
        "domain_checks",
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column(
            "check_type",
            sa.Enum(
                "ownership",
                "routing",
                "certificate",
                "origin",
                name="check_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "passing",
                "failing",
                name="check_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "check_type IN ('ownership', 'routing', 'certificate', 'origin')",
            name=op.f("ck_domain_checks_check_type"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'passing', 'failing')",
            name=op.f("ck_domain_checks_check_status"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_domain_checks_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain_checks")),
        sa.UniqueConstraint(
            "domain_id", "check_type", name=op.f("uq_domain_checks_domain_id_check_type")
        ),
    )
    op.create_table(
        "domain_events",
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_domain_events_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_domain_events_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain_events")),
    )
    with op.batch_alter_table("domain_events", schema=None) as batch_op:
        batch_op.create_index(
            "ix_domain_events_application_created", ["application_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_domain_events_domain_created", ["domain_id", "created_at"], unique=False
        )

    op.create_table(
        "ownership_claims",
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("txt_record_name", sa.String(length=253), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("cname_target", sa.String(length=253), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "verified",
                "revoked",
                name="claim_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("verification_method", sa.String(length=32), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'revoked')",
            name=op.f("ck_ownership_claims_claim_status"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_ownership_claims_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ownership_claims")),
        sa.UniqueConstraint("token", name=op.f("uq_ownership_claims_token")),
    )
    with op.batch_alter_table("ownership_claims", schema=None) as batch_op:
        batch_op.create_index(
            "ux_ownership_claims_one_live",
            ["domain_id"],
            unique=True,
            postgresql_where=sa.text("status <> 'revoked'"),
            sqlite_where=sa.text("status <> 'revoked'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("ownership_claims", schema=None) as batch_op:
        batch_op.drop_index(
            "ux_ownership_claims_one_live",
            postgresql_where=sa.text("status <> 'revoked'"),
            sqlite_where=sa.text("status <> 'revoked'"),
        )

    op.drop_table("ownership_claims")
    with op.batch_alter_table("domain_events", schema=None) as batch_op:
        batch_op.drop_index("ix_domain_events_domain_created")
        batch_op.drop_index("ix_domain_events_application_created")

    op.drop_table("domain_events")
    op.drop_table("domain_checks")
    with op.batch_alter_table("verified_origins", schema=None) as batch_op:
        batch_op.drop_index(
            "ux_verified_origins_one_active",
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active"),
        )

    op.drop_table("verified_origins")
    with op.batch_alter_table("domains", schema=None) as batch_op:
        batch_op.drop_index(
            "ux_domains_hostname_live",
            postgresql_where=sa.text("deleted_at IS NULL"),
            sqlite_where=sa.text("deleted_at IS NULL"),
        )
        batch_op.drop_index("ix_domains_purge_after")
        batch_op.drop_index("ix_domains_application_status")
        batch_op.drop_index("ix_domains_application_reference")

    op.drop_table("domains")
    with op.batch_alter_table("api_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_credentials_key_prefix"))
        batch_op.drop_index(batch_op.f("ix_api_credentials_application_id"))

    op.drop_table("api_credentials")
    op.drop_table("applications")
