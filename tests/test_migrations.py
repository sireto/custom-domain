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
