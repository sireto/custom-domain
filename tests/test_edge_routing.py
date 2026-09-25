"""Routing configuration, the assert endpoint, and an end-to-end run through a real Caddy."""

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.db.session import get_session
from app.edge.assertion import HEADER as ASSERTION_HEADER
from app.edge.assertion import AssertionInvalid, parse_keys, verify
from app.edge.config import (
    EDGE_HOST_HEADER,
    EDGE_SNI_HEADER,
    STRIPPED_REQUEST_HEADERS,
    build_apps,
)
from app.edge.settings import ASSERT_PATH, EdgeSettings
from app.main import create_app
from app.models import ApplicationStatus, CheckStatus, CheckType, DomainStatus
from app.services.applications import (
    activate_origin,
    record_origin_verification,
    register_origin,
    set_application_status,
)
from app.services.domains import (
    claim_domain,
    delete_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)

KEY_SPEC = "k1:" + "s" * 40
KEYS = parse_keys(KEY_SPEC)


def _settings(**overrides):
    base = dict(
        reconcile_enabled=True,
        legacy_api_enabled=False,
        assertion_keys=(("k1", "s" * 40),),
        ask_trusted_hosts=("testclient", "127.0.0.1", "::1"),
    )
    base.update(overrides)
    return EdgeSettings(**base)


def _ready(session, application, hostname, reference):
    domain = claim_domain(session, application, hostname, reference)
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    return domain


def _with_origin(session, application, host, port, scheme="http"):
    origin = register_origin(session, application, host=host, scheme=scheme, port=port)
    record_origin_verification(session, origin, verified=True)
    activate_origin(session, origin)
    session.commit()
    return origin


# --- configuration shape -------------------------------------------------------


def test_route_strips_headers_then_asserts_then_proxies(session, make_application):
    acme = make_application("acme")
    _with_origin(session, acme, "app.acme.example", 443, scheme="https")
    _ready(session, acme, "forms.customer.example", "ws_1")
    settings = _settings(assert_upstream="localhost:9000")
    apps = build_apps(session, settings)
    route = [r for r in apps["http"]["servers"]["edge"]["routes"] if r["@id"] == "app-acme"][0]
    strip, assert_step, proxy = route["handle"]
    assert strip == {"handler": "headers", "request": {"delete": list(STRIPPED_REQUEST_HEADERS)}}
    assert ASSERTION_HEADER in STRIPPED_REQUEST_HEADERS
    assert assert_step["upstreams"] == [{"dial": "localhost:9000"}]
    assert assert_step["rewrite"] == {"method": "GET", "uri": ASSERT_PATH}
    sent = assert_step["headers"]["request"]["set"]
    assert sent[EDGE_HOST_HEADER] == ["{http.request.host}"]
    assert sent[EDGE_SNI_HEADER] == ["{http.request.tls.server_name}"]
    copied = assert_step["handle_response"][0]
    assert copied["match"] == {"status_code": [2]}
    assert copied["routes"][0]["handle"][0]["request"]["set"][ASSERTION_HEADER] == [
        "{http.reverse_proxy.header.X-Custom-Domain-Assertion}"
    ]
    assert proxy["upstreams"] == [{"dial": "203.0.113.10:443"}]  # pinned address
    assert proxy["headers"]["request"]["set"]["Host"] == ["{http.request.host}"]
    assert proxy["headers"]["request"]["set"]["X-Forwarded-Host"] == ["{http.request.host}"]
    assert proxy["transport"] == {"protocol": "http", "tls": {"server_name": "app.acme.example"}}
    assert apps["http"]["servers"]["edge"]["strict_sni_host"] is True
    assert (
        "strict_sni_host"
        not in build_apps(session, _settings(disable_https=True))["http"]["servers"]["edge"]
    )


def test_settings_require_signing_keys_when_edge_enabled():
    from app.edge.settings import EdgeConfigurationError

    with pytest.raises(EdgeConfigurationError, match="EDGE_ASSERTION_KEYS"):
        EdgeSettings.from_env({"ENABLE_LEGACY_API": "false"})
    settings = EdgeSettings.from_env(
        {
            "ENABLE_LEGACY_API": "false",
            "EDGE_ASSERTION_KEYS": "2:" + "b" * 32 + ",1:" + "a" * 32,
            "EDGE_ASSERTION_TTL": "30",
        }
    )
    assert settings.active_key() == ("2", b"b" * 32)
    assert set(settings.signing_keys()) == {"1", "2"} and settings.assertion_ttl == 30
    with pytest.raises(EdgeConfigurationError, match="EDGE_ASSERTION_KEYS"):
        EdgeSettings.from_env({"ENABLE_LEGACY_API": "false", "EDGE_ASSERTION_KEYS": "1:short"})
    # Legacy mode does not need a key.
    assert EdgeSettings.from_env({}).assertion_keys == ()


# --- assert endpoint -----------------------------------------------------------


@pytest.fixture
def client(session_factory, monkeypatch):
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("EDGE_RECONCILE_ENABLED", "false")
    monkeypatch.setenv("DNS_WORKER_ENABLED", "false")
    app = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    with httpx.Client(transport=httpx.WSGITransport(app=None)) if False else _TestClient(app) as c:
        c.app.state.edge_settings = _settings()
        yield c


from fastapi.testclient import TestClient as _TestClient  # noqa: E402


def _assert_headers(host, sni=None, rid="req-1"):
    headers = {EDGE_HOST_HEADER: host, "X-Custom-Domain-Edge-Request-Id": rid}
    if sni is not None:
        headers[EDGE_SNI_HEADER] = sni
    return headers


def test_assert_endpoint_signs_only_serveable_hosts(client, session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    _with_origin(session, acme, "app.acme.example", 443, "https")
    _with_origin(session, globex, "app.globex.example", 443, "https")
    a = _ready(session, acme, "a.customer.example", "ws_a")
    g = _ready(session, globex, "g.customer.example", "ws_g")
    pending = claim_domain(session, acme, "pending.customer.example", "ws_p")
    session.commit()

    response = client.get(
        ASSERT_PATH, headers=_assert_headers("A.Customer.Example", "a.customer.example", "rid-1")
    )
    assert response.status_code == 200
    assertion = verify(
        response.headers[ASSERTION_HEADER], KEYS, expected_application_id=str(acme.id)
    )
    assert assertion.reference == "ws_a" and assertion.domain_id == str(a.id)
    assert assertion.hostname == "a.customer.example" and assertion.request_id == "rid-1"

    response = client.get(ASSERT_PATH, headers=_assert_headers("g.customer.example"))
    other = verify(response.headers[ASSERTION_HEADER], KEYS)
    assert other.application_id == str(globex.id) and other.reference == "ws_g"
    assert other.domain_id == str(g.id)

    # Not serveable, unknown, invalid, or a Host that does not match the SNI.
    assert (
        client.get(ASSERT_PATH, headers=_assert_headers("pending.customer.example")).status_code
        == 403
    )
    assert (
        client.get(ASSERT_PATH, headers=_assert_headers("nobody.customer.example")).status_code
        == 403
    )
    assert client.get(ASSERT_PATH, headers=_assert_headers("*.customer.example")).status_code == 403
    assert (
        client.get(
            ASSERT_PATH, headers=_assert_headers("a.customer.example", "g.customer.example")
        ).status_code
        == 403
    )
    assert client.get(ASSERT_PATH).status_code == 403

    # Provisioning (claim verified) hosts are routed only for the workspace probe path.
    from app.services.domains import mark_claim_verified
    from app.services.domains import transition_status as _transition

    mark_claim_verified(session, pending)
    _transition(session, pending, DomainStatus.PROVISIONING)
    session.commit()
    probe_headers = {
        **_assert_headers("pending.customer.example"),
        "X-Forwarded-Uri": "/.well-known/custom-domain-workspace?x=1",
    }
    assert client.get(ASSERT_PATH, headers=probe_headers).status_code == 200
    page_headers = {**_assert_headers("pending.customer.example"), "X-Forwarded-Uri": "/dashboard"}
    assert client.get(ASSERT_PATH, headers=page_headers).status_code == 403
    _transition(session, pending, DomainStatus.PENDING_DNS)
    session.commit()
    placeholder = _assert_headers("g.customer.example", "{http.request.tls.server_name}")
    assert client.get(ASSERT_PATH, headers=placeholder).status_code == 200  # plain HTTP
    assert pending.status == DomainStatus.PENDING_DNS

    # Suspending the application or deleting the domain stops routing at once.
    set_application_status(session, acme, ApplicationStatus.SUSPENDED)
    session.commit()
    assert client.get(ASSERT_PATH, headers=_assert_headers("a.customer.example")).status_code == 403
    set_application_status(session, acme, ApplicationStatus.ACTIVE)
    delete_domain(session, acme, a.id)
    session.commit()
    assert client.get(ASSERT_PATH, headers=_assert_headers("a.customer.example")).status_code == 403


def test_assert_endpoint_fails_closed_without_keys_or_trust(client, session, make_application):
    acme = make_application("acme")
    _with_origin(session, acme, "app.acme.example", 443, "https")
    _ready(session, acme, "a.customer.example", "ws_a")
    client.app.state.edge_settings = _settings(assertion_keys=())
    assert client.get(ASSERT_PATH, headers=_assert_headers("a.customer.example")).status_code == 503
    client.app.state.edge_settings = _settings(ask_trusted_hosts=("10.0.0.1",))
    assert client.get(ASSERT_PATH, headers=_assert_headers("a.customer.example")).status_code == 403


# --- end to end through Caddy --------------------------------------------------


class RecordingOrigin:
    def __init__(self, name):
        self.name = name
        self.requests = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                origin.requests.append({"path": self.path, "headers": dict(self.headers)})
                body = f"hello from {origin.name}".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


from tests.caddy_support import caddy_required  # noqa: E402


@caddy_required()
def test_two_applications_are_never_confused_through_real_caddy(
    session, session_factory, make_application, monkeypatch, tmp_path
):
    import uvicorn

    acme = make_application("acme")
    globex = make_application("globex")
    origin_a = RecordingOrigin("acme")
    origin_g = RecordingOrigin("globex")
    _with_origin(session, acme, "localhost", origin_a.port)
    _with_origin(session, globex, "localhost", origin_g.port)
    _ready(session, acme, "one.acme-customer.example", "ws_acme_1")
    _ready(session, acme, "two.acme-customer.example", "ws_acme_2")
    _ready(session, globex, "one.globex-customer.example", "ws_globex_1")
    stale = claim_domain(session, globex, "stale.globex-customer.example", "ws_stale")
    session.commit()

    # The management API, served for real so Caddy can call it.
    api_port = _free_port()
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("EDGE_RECONCILE_ENABLED", "false")
    monkeypatch.setenv("DNS_WORKER_ENABLED", "false")
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")  # the test origins are on loopback
    app = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    settings = _settings(
        disable_https=True,
        https_port=_free_port(),
        assert_upstream=f"127.0.0.1:{api_port}",
        ask_trusted_hosts=("127.0.0.1", "::1"),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    )
    api_thread = threading.Thread(target=server.run, daemon=True)
    api_thread.start()
    assert _wait_port(api_port)
    app.state.edge_settings = settings

    config = {"admin": {"disabled": True}, "apps": build_apps(session, settings)}
    config_file = tmp_path / "caddy.json"
    config_file.write_text(json.dumps(config))
    caddy = subprocess.Popen(
        ["caddy", "run", "--config", str(config_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path),
        },
    )
    try:
        assert _wait_port(settings.https_port), caddy.stderr.read().decode()[-2000:]
        base = f"http://127.0.0.1:{settings.https_port}"
        forged = {
            ASSERTION_HEADER: "v1.k1.forged.forged",
            "X-Custom-Domain-Reference": "ws_globex_1",
        }

        r = httpx.get(
            f"{base}/dashboard?x=1", headers={"Host": "one.acme-customer.example", **forged}
        )
        assert r.status_code == 200 and r.text == "hello from acme"
        received = origin_a.requests[-1]["headers"]
        assertion = verify(received[ASSERTION_HEADER], KEYS, expected_application_id=str(acme.id))
        assert (
            assertion.reference == "ws_acme_1" and assertion.hostname == "one.acme-customer.example"
        )
        assert assertion.request_id and assertion.request_id != "unknown"
        assert received["Host"] == "one.acme-customer.example"
        assert received["X-Forwarded-Host"] == "one.acme-customer.example"
        assert received["X-Forwarded-Proto"] == "http"
        assert "X-Custom-Domain-Reference" not in received
        assert origin_a.requests[-1]["path"] == "/dashboard?x=1"

        r = httpx.get(f"{base}/", headers={"Host": "two.acme-customer.example"})
        assert r.text == "hello from acme"
        assert (
            verify(origin_a.requests[-1]["headers"][ASSERTION_HEADER], KEYS).reference
            == "ws_acme_2"
        )

        r = httpx.get(f"{base}/", headers={"Host": "one.globex-customer.example"})
        assert r.text == "hello from globex"
        g_assertion = verify(origin_g.requests[-1]["headers"][ASSERTION_HEADER], KEYS)
        assert (
            g_assertion.application_id == str(globex.id) and g_assertion.reference == "ws_globex_1"
        )
        with pytest.raises(AssertionInvalid) as wrong:
            verify(
                origin_g.requests[-1]["headers"][ASSERTION_HEADER],
                KEYS,
                expected_application_id=str(acme.id),
            )
        assert wrong.value.code == "wrong_application"

        # Unknown and not-yet-ready hostnames never reach an origin.
        before = (len(origin_a.requests), len(origin_g.requests))
        for host in (
            "stale.globex-customer.example",
            "nobody.example",
            "one.acme-customer.example.evil",
        ):
            r = httpx.get(f"{base}/", headers={"Host": host})
            assert r.status_code == 404, host
        assert (len(origin_a.requests), len(origin_g.requests)) == before
        assert stale.status == DomainStatus.PENDING_DNS

        # Deleting a domain stops routing on the next request (assert-time lookup).
        two = [d for d in acme.domains if d.hostname == "two.acme-customer.example"][0]
        delete_domain(session, acme, two.id)
        session.commit()
        r = httpx.get(f"{base}/", headers={"Host": "two.acme-customer.example"})
        assert r.status_code == 403
        assert len(origin_a.requests) == before[0]
    finally:
        caddy.terminate()
        caddy.wait(timeout=10)
        server.should_exit = True
        api_thread.join(timeout=10)
        origin_a.close()
        origin_g.close()
        assert uuid  # keep import used for readers of recorded ids
