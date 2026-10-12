from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.db import migrate
from app.db.base import Base


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "render_as_batch": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], diff


def test_downgrade_and_upgrade_roundtrip(engine, database_url):
    migrate.downgrade(database_url, "base")
    assert "domains" not in inspect(engine).get_table_names()
    migrate.upgrade(database_url)
    tables = set(inspect(engine).get_table_names())
    assert {
        "applications",
        "verified_origins",
        "api_credentials",
        "domains",
        "ownership_claims",
        "domain_checks",
        "domain_events",
    } <= tables


def test_existing_credentials_keep_full_access_across_0013(engine, database_url):
    """0013 adds api_credentials.scope; a credential issued before it stays full."""
    import uuid

    from sqlalchemy import Column, DateTime, MetaData, String, Table, Uuid, select

    from app.models.types import utcnow

    # Only the columns the previous release has, so nothing writes scope.
    old_credentials = Table(
        "api_credentials",
        MetaData(),
        Column("id", Uuid),
        Column("application_id", Uuid),
        Column("label", String),
        Column("key_prefix", String),
        Column("key_hash", String),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
    )
    applications = Base.metadata.tables["applications"]
    application_id, now = uuid.uuid4(), utcnow()

    migrate.downgrade(database_url, "0012")
    try:
        assert "scope" not in {c["name"] for c in inspect(engine).get_columns("api_credentials")}
        with engine.begin() as conn:
            conn.execute(
                applications.insert().values(
                    id=application_id,
                    slug="old",
                    name="Old",
                    status="active",
                    cname_target="old.edge.example.net",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                old_credentials.insert().values(
                    id=uuid.uuid4(),
                    application_id=application_id,
                    label="old",
                    key_prefix="cd_old",
                    key_hash="0" * 64,
                    created_at=now,
                    updated_at=now,
                )
            )
        migrate.upgrade(database_url)
        scope = Base.metadata.tables["api_credentials"].c.scope
        with engine.connect() as conn:
            assert [str(s) for s in conn.execute(select(scope)).scalars()] == ["full"]
        migrate.downgrade(database_url, "0012")
        assert "scope" not in {c["name"] for c in inspect(engine).get_columns("api_credentials")}
    finally:
        migrate.upgrade(database_url)
        with engine.begin() as conn:
            conn.execute(Base.metadata.tables["api_credentials"].delete())
            conn.execute(applications.delete())


def test_0015_adds_the_signer_table_and_connect_start(engine, database_url):
    """0015 is additive: the previous release's tables and columns are untouched."""
    migrate.downgrade(database_url, "0014")
    try:
        inspector = inspect(engine)
        assert "domain_connect_signers" not in inspector.get_table_names()
        assert "connect_started_at" not in {c["name"] for c in inspector.get_columns("domains")}
    finally:
        migrate.upgrade(database_url)
    inspector = inspect(engine)
    assert "domain_connect_signers" in inspector.get_table_names()
    column = next(c for c in inspector.get_columns("domains") if c["name"] == "connect_started_at")
    assert column["nullable"]
