"""The operator portal: sign-in, CSRF, and every action the command line offers."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import ApplicationStatus, DomainStatus
from app.portal.auth import LoginLimiter, PortalSettings, Sessions
from app.services import applications as app_service
from app.services.domains import find_live_by_hostname, get_domain

PASSWORD = "correct-horse-battery-staple"


def make_portal_client(session_factory, monkeypatch):
    """A signed-out test client for the portal, with DNS answered locally."""
    from app.db.session import get_session
    from app.main import create_app

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("PORTAL_PASSWORD", PASSWORD)
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")
    app = create_app()
    # Pages that show DNS status look names up in public DNS; tests answer locally.
    app.state.portal_resolve = fake_resolve
    app.state.portal_probe = lambda name, address, settings: (204, "1")

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(session_factory, monkeypatch):
    yield from make_portal_client(session_factory, monkeypatch)


PUBLISHED = {"edge.example.net": ["93.184.216.34"], "edge2.example.net": ["93.184.216.34"]}


def fake_resolve(name, dns_settings):
    if name not in PUBLISHED:
        raise LookupError(f"{name} does not exist in public DNS")
    return PUBLISHED[name]


def sign_in(client: TestClient, password: str = PASSWORD):
    response = client.post(
        "/portal/login", data={"password": password, "next": "/portal"}, follow_redirects=False
    )
    return response


def csrf_of(client: TestClient) -> str:
    return Sessions(client.app.state.portal_settings).read(client.cookies.get("cd_portal"))["csrf"]


# --- sign-in and session -------------------------------------------------------------


def test_portal_requires_sign_in_and_rejects_bad_passwords(client):
    page = client.get("/portal", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/portal/login")
    assert client.get("/portal/login").status_code == 200

    assert sign_in(client, "wrong").status_code == 401
    assert "cd_portal" not in client.cookies
    ok = sign_in(client)
    assert ok.status_code == 303 and ok.headers["location"] == "/portal"
    assert "cd_portal" in client.cookies

    home = client.get("/portal")
    assert home.status_code == 200 and "Overview" in home.text
    assert home.headers["Cache-Control"] == "no-store"
    assert home.headers["X-Frame-Options"] == "DENY"

    # Sign out needs the CSRF token; without it the session stays.
    assert client.post("/portal/logout", data={}).status_code == 403
    out = client.post("/portal/logout", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/portal", follow_redirects=False).status_code == 303


def test_portal_sign_in_is_rate_limited(client):
    for _ in range(5):
        assert sign_in(client, "wrong").status_code == 401
    assert sign_in(client, "wrong").status_code == 429
    assert sign_in(client).status_code == 429  # even the right password, until the window passes
    client.app.state.portal_limiter.reset("testclient")
    assert sign_in(client).status_code == 303


def test_portal_open_redirects_are_refused(client):
    response = client.post(
        "/portal/login",
        data={"password": PASSWORD, "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/portal"


def test_session_cookie_is_signed_and_expires():
    settings = PortalSettings.from_env({"PORTAL_PASSWORD": PASSWORD})
    sessions = Sessions(settings, ttl=60)
    value, csrf = sessions.issue(now=1000)
    assert sessions.read(value, now=1030)["csrf"] == csrf
    assert sessions.read(value, now=1061) is None
    assert sessions.read(value[:-2] + "xx", now=1030) is None  # tampered signature
    assert sessions.read("garbage", now=1030) is None
    other = Sessions(PortalSettings.from_env({"PORTAL_PASSWORD": "another-long-password-1"}))
    assert other.read(value, now=1030) is None

    limiter = LoginLimiter(max_failures=2, window=100)
    limiter.record_failure("a", now=0)
    limiter.record_failure("a", now=1)
    assert not limiter.allowed("a", now=50) and limiter.allowed("a", now=150)
    assert limiter.allowed("b", now=50)


def test_portal_is_disabled_without_a_password(session_factory, monkeypatch):
    from app.main import create_app

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.delenv("PORTAL_PASSWORD", raising=False)
    with TestClient(create_app()) as c:
        assert c.get("/portal", follow_redirects=False).status_code == 503
        assert c.post("/portal/login", data={"password": "x"}).status_code == 503
    monkeypatch.setenv("PORTAL_PASSWORD", "short")  # too short: still disabled
    with TestClient(create_app()) as c:
        assert c.get("/portal/login").status_code == 503


# --- the actions -----------------------------------------------------------------------


def test_portal_runs_the_operator_actions(client, session, monkeypatch):
    from tests.test_origin_verification import TokenServer

    sign_in(client)
    csrf = csrf_of(client)

    # Application
    bad = client.post(
        "/portal/applications",
        data={"csrf": csrf, "slug": "Bad Slug", "name": "x", "cname_target": "edge.example.net"},
    )
    assert bad.status_code == 400 and "Slug must be" in bad.text
    created = client.post(
        "/portal/applications",
        data={"csrf": csrf, "slug": "acme", "name": "Acme", "cname_target": "edge.example.net"},
        follow_redirects=False,
    )
    assert created.status_code == 303 and created.headers["location"].startswith(
        "/portal/applications/acme"
    )
    page = client.get("/portal/applications/acme")
    assert page.status_code == 200 and "edge.example.net" in page.text
    assert client.get("/portal/applications/nope").status_code == 404

    # Settings: CNAME target, workspace probe, status
    assert (
        client.post(
            "/portal/applications/acme/cname-target",
            data={"csrf": csrf, "cname_target": "Edge2.Example.Net."},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/portal/applications/acme/workspace-probe",
            data={"csrf": csrf, "enabled": "false"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        assert acme.cname_target == "edge2.example.net" and not acme.workspace_probe_enabled

    # Origin: register shows the token once; verification runs against a real listener
    server = TokenServer()
    try:
        registered = client.post(
            "/portal/applications/acme/origins",
            data={"csrf": csrf, "host": "localhost", "scheme": "http", "port": str(server.port)},
        )
        # The redirect lands on the origins tab, which shows the token with instructions.
        assert registered.status_code == 200 and "Origin registered" in registered.text
        assert "custom-domain-origin-verification" in registered.text
        with session_scope(session) as s:
            origin = app_service.list_origins(s, app_service.get_application_by_slug(s, "acme"))[0]
            token = origin.verification_token
            origin_id = origin.id
        assert token in registered.text
        server.body = b"wrong"
        failed = client.post(
            f"/portal/applications/acme/origins/{origin_id}/verify",
            data={"csrf": csrf, "activate": "true"},
        )
        assert failed.status_code == 400 and "token" in failed.text
        server.body = token.encode()
        verified = client.post(
            f"/portal/applications/acme/origins/{origin_id}/verify",
            data={"csrf": csrf, "activate": "true"},
            follow_redirects=False,
        )
        assert verified.status_code == 303
    finally:
        server.close()
    with session_scope(session) as s:
        origin = app_service.list_origins(s, app_service.get_application_by_slug(s, "acme"))[0]
        assert origin.status.value == "verified" and origin.is_active

    # Credential: shown once, then rotate and revoke
    issued = client.post(
        "/portal/applications/acme/credentials", data={"csrf": csrf, "label": "backend"}
    )
    assert issued.status_code == 200 and "cd_" in issued.text and "shown only once" in issued.text
    with session_scope(session) as s:
        credential = app_service.list_credentials(
            s, app_service.get_application_by_slug(s, "acme")
        )[0]
        credential_id = credential.id
    rotated = client.post(
        f"/portal/applications/acme/credentials/{credential_id}/rotate",
        data={"csrf": csrf, "grace_hours": "1"},
    )
    assert rotated.status_code == 200 and "cd_" in rotated.text
    with session_scope(session) as s:
        credentials = app_service.list_credentials(
            s, app_service.get_application_by_slug(s, "acme")
        )
        new_id = [c for c in credentials if c.id != credential_id][0].id
    assert (
        client.post(
            f"/portal/applications/acme/credentials/{new_id}/revoke",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )

    # Domains: register, recheck (rate limited on the second try), re-issue, delete
    assert (
        client.post(
            "/portal/applications/acme/domains",
            data={"csrf": csrf, "hostname": "forms.customer.example", "reference": "ws_1"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with session_scope(session) as s:
        domain = find_live_by_hostname(s, "forms.customer.example")
        domain_id = domain.id
    detail = client.get(f"/portal/applications/acme/domains/{domain_id}")
    assert (
        detail.status_code == 200
        and "_custom-domain-challenge.forms.customer.example" in detail.text
    )
    assert (
        client.post(
            f"/portal/applications/acme/domains/{domain_id}/recheck",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    limited = client.post(
        f"/portal/applications/acme/domains/{domain_id}/recheck", data={"csrf": csrf}
    )
    assert limited.status_code == 400 and "rechecked less than" in limited.text
    assert (
        client.post(
            f"/portal/applications/acme/domains/{domain_id}/reissue",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/portal/applications/acme/domains/{domain_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        gone = get_domain(s, acme, domain_id, include_deleted=True)
        assert gone.status == DomainStatus.DELETING
    assert client.get("/portal/applications/acme/domains?status=deleting").status_code == 200

    # Status, then the listing pages
    assert (
        client.post(
            "/portal/applications/acme/status",
            data={"csrf": csrf, "status": "suspended"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with session_scope(session) as s:
        assert app_service.get_application_by_slug(s, "acme").status == ApplicationStatus.SUSPENDED
    assert "acme" in client.get("/portal/applications").text
    assert client.get("/portal/edge").status_code == 200

    # Every action refuses a missing or foreign CSRF token.
    assert (
        client.post(
            "/portal/applications", data={"slug": "x", "name": "x", "cname_target": "e.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/portal/applications/acme/domains/{uuid.uuid4()}/delete", data={"csrf": "nope"}
        ).status_code
        == 403
    )


def test_portal_legacy_import_and_maintenance(client, session, tmp_path):
    from app.caddy import saas_template

    sign_in(client)
    csrf = csrf_of(client)
    client.post(
        "/portal/applications",
        data={"csrf": csrf, "slug": "acme", "name": "Acme", "cname_target": "edge.example.net"},
    )
    config = saas_template.https_template()
    for hostname in ("one.customer.example", "two.customer.example"):
        config = saas_template.add_https_domain(hostname, "app.acme.example:443", template=config)
    body = json.dumps(config).encode()

    dry = client.post(
        "/portal/legacy",
        data={
            "csrf": csrf,
            "application": "acme",
            "references": json.dumps({"one.customer.example": "ws_one"}),
            "dry_run": "true",
            "grandfather": "true",
        },
        files={"config": ("caddy.json", body, "application/json")},
    )
    assert dry.status_code == 200 and "Dry run" in dry.text and "skipped" in dry.text
    with session_scope(session) as s:
        assert find_live_by_hostname(s, "one.customer.example") is None  # nothing written

    real = client.post(
        "/portal/legacy",
        data={
            "csrf": csrf,
            "application": "acme",
            "references": json.dumps({"one.customer.example": "ws_one"}),
            "hostname_as_reference": "true",
            "grandfather": "true",
        },
        files={"config": ("caddy.json", body, "application/json")},
    )
    assert real.status_code == 200 and "Imported into acme" in real.text
    with session_scope(session) as s:
        one = find_live_by_hostname(s, "one.customer.example")
        two = find_live_by_hostname(s, "two.customer.example")
        assert one.reference == "ws_one" and two.reference == "two.customer.example"

    assert (
        client.post(
            "/portal/maintenance/purge-tombstones", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    bad = client.post(
        "/portal/legacy",
        data={"csrf": csrf, "application": "acme", "references": "not json"},
        files={"config": ("caddy.json", body, "application/json")},
    )
    assert bad.status_code == 400 and "could not be read" in bad.text


class session_scope:
    """A fresh session that sees committed state, closed on exit."""

    def __init__(self, session):
        self.factory = session.get_bind

    def __enter__(self):
        from sqlalchemy.orm import Session

        self.session = Session(bind=self.factory())
        return self.session

    def __exit__(self, *exc):
        self.session.close()


def test_portal_allowlist_applies_to_public_addresses(session_factory, monkeypatch):
    """Private peers (tunnel, Docker network) always may; public ones only when allowed,
    attributed through X-Forwarded-For only when the peer is a trusted edge. The
    addresses are globally routable ones: documentation ranges count as non-global."""
    from app.db.session import get_session
    from app.main import create_app

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("PORTAL_PASSWORD", PASSWORD)
    monkeypatch.setenv("PORTAL_ALLOWED_IPS", "93.184.216.34, 151.101.0.0/16")
    monkeypatch.setenv("EDGE_ASK_TRUSTED_HOSTS", "127.0.0.1,10.90.0.0/16")
    app = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    with TestClient(app) as _lifespan:
        # A public peer not on the list is refused before the password is even checked.
        refused = TestClient(app, client=("8.8.8.8", 1000))
        assert refused.get("/portal/login").status_code == 403
        assert "not on the operator allowlist" in refused.get("/portal/login").text
        assert refused.post("/portal/login", data={"password": PASSWORD}).status_code == 403
        # A public peer on the list may sign in.
        allowed = TestClient(app, client=("151.101.65.140", 1000))
        assert allowed.get("/portal/login").status_code == 200
        assert sign_in(allowed).status_code == 303
        # Through the edge (trusted peer): the forwarded address decides, and the cookie is
        # Secure when the edge says HTTPS.
        via_edge = TestClient(app, client=("10.90.0.5", 1000))
        headers = {"X-Forwarded-For": "8.8.8.8", "X-Forwarded-Proto": "https"}
        assert via_edge.get("/portal/login", headers=headers).status_code == 403
        headers["X-Forwarded-For"] = "93.184.216.34"
        assert via_edge.get("/portal/login", headers=headers).status_code == 200
        signed = via_edge.post(
            "/portal/login",
            data={"password": PASSWORD, "next": "/portal"},
            headers=headers,
            follow_redirects=False,
        )
        assert signed.status_code == 303 and "Secure" in signed.headers["set-cookie"]
        # An untrusted peer cannot borrow an allowed address through the header.
        spoof = TestClient(app, client=("8.8.8.8", 1000))
        assert (
            spoof.get("/portal/login", headers={"X-Forwarded-For": "93.184.216.34"}).status_code
            == 403
        )
        # Private peers (the SSH tunnel arrives from the Docker bridge) always may.
        local = TestClient(app, client=("172.18.0.1", 1000))
        assert local.get("/portal/login").status_code == 200
        # The rate limit is per attributed address: the refused peer's failures do not
        # count against the allowed one.
        for _ in range(5):
            allowed.post("/portal/login", data={"password": "wrong"})
        assert allowed.post("/portal/login", data={"password": "wrong"}).status_code == 429
        assert via_edge.get("/portal/login", headers=headers).status_code == 200
