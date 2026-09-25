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


@pytest.fixture(autouse=True)
def pinned_origin_addresses(monkeypatch):
    """Pin origin names to fixed addresses so the edge config never touches DNS.

    Documentation names map to public documentation addresses, ``localhost``
    to the loopback address the test origins listen on, and IP literals to
    themselves. ``pinned_dial`` itself is covered by tests/test_origin_verification.py.
    """
    import ipaddress

    from app.edge import config as edge_config
    from app.services.origin_verification import OriginVerificationFailed

    table = {
        "app.acme.example": "203.0.113.10",
        "app.globex.example": "203.0.113.20",
        "globex.internal": "198.51.100.7",
        "localhost": "127.0.0.1",
    }
    private_networks = [
        ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]

    def fake_pinned_dial(host, port, *, allow_private=False):
        address = table.get(host)
        if address is None:
            try:
                address = str(ipaddress.ip_address(host))
            except ValueError:
                raise OriginVerificationFailed(
                    "dns_resolution_failed", f"{host} does not resolve"
                ) from None
        # Documentation ranges count as non-global in ipaddress; block only the
        # loopback and RFC 1918 networks the tests actually use for "private".
        parsed = ipaddress.ip_address(address)
        private = parsed.is_loopback or any(parsed in net for net in private_networks)
        if private and not allow_private:
            raise OriginVerificationFailed("private_address_blocked", f"{host} is private")
        dial = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        return dial, host

    monkeypatch.setattr(edge_config, "pinned_dial", fake_pinned_dial)
    return table
