"""Test database fixtures.

Tests run on a temporary SQLite file by default. Set ``TEST_DATABASE_URL`` to a
PostgreSQL URL to run the same suite against the production backend. The
schema is always created through Alembic so the migrations are exercised too.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

import app.models  # noqa: F401
from app.db import migrate
from app.db.base import Base
from app.db.session import create_engine_from_url, make_session_factory
from app.services.applications import create_application


@pytest.fixture(scope="session")
def database_url(tmp_path_factory) -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    return f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"


def _reset_schema(engine) -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(scope="session")
def engine(database_url):
    engine = create_engine_from_url(database_url)
    _reset_schema(engine)
    migrate.upgrade(database_url)
    yield engine
    _reset_schema(engine)
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def session(engine, session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def make_application(session):
    def _make(slug: str = "acme", *, name: str | None = None, cname_target: str | None = None):
        application = create_application(
            session,
            slug=slug,
            name=name or slug.title(),
            cname_target=cname_target or f"{slug}.edge.example.net",
        )
        session.commit()
        return application

    return _make
