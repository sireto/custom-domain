"""Engine and session construction.

The authoritative store is a relational database selected through
``DATABASE_URL``. PostgreSQL is the production target; SQLite is supported for
local development and tests so the model can be exercised without extra
services. See docs/data-model.md for the rationale.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///data/custom_domain.db"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_engine_from_url(url: str) -> Engine:
    if url.startswith("sqlite"):
        return _create_sqlite_engine(url)
    return create_engine(url, pool_pre_ping=True)


def _create_sqlite_engine(url: str) -> Engine:
    path = url.removeprefix("sqlite:///")
    if path and path != ":memory:" and not path.startswith("file:"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection, _record):  # pragma: no cover - driver glue
        # Let SQLAlchemy control transactions so SAVEPOINTs behave like on
        # PostgreSQL; pysqlite's legacy transaction handling breaks them.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(conn):  # pragma: no cover - driver glue
        conn.exec_driver_sql("BEGIN")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine_from_url(get_database_url())
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(get_engine())
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding one session per request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
