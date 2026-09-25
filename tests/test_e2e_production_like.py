"""Production-like end to end: two applications, real Caddy, on-demand TLS.

Runs the whole path with the real Caddy binary: the reconciler applies the
derived configuration, Caddy issues certificates on demand from its internal
CA after asking the API, the checks worker (with in-memory DNS) drives
domains to ready through the real HTTPS readiness and workspace probes, and
HTTPS requests through the edge show the right workspace for each hostname.
Wrong host, stale claim and revoked domain are rejected. Skipped when no
``caddy`` binary is installed.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import uuid

import pytest
import uvicorn
from custom_domain import Client, CustomDomainMiddleware
from custom_domain.errors import AuthenticationError, ConflictError, NotFoundError
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.db.session import get_session
from app.dns.worker import run_due_checks
from app.edge.caddy_client import CaddyClient
from app.edge.config import build_bootstrap
from app.edge.reconcile import Reconciler
from app.edge.settings import EdgeSettings
from app.main import create_app
from app.services.applications import (
    activate_origin,
    issue_credential,
    register_origin,
)
from app.services.domains import reissue_claim
from app.services.edge_checks import SystemEdgeProber
from app.services.origin_verification import (
    WELL_KNOWN_PATH,
    OriginVerificationFailed,
    verify_origin,
)
from tests.test_dns_checks import FakeResolver

pytestmark = pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy binary not installed")

KEYS = (("1", "e" * 40),)
EDGE_TOKEN = "edge-token-" + "t" * 30


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _serve(app: FastAPI, *, proxy_headers: bool = True) -> tuple[uvicorn.Server, int]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=proxy_headers
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    assert _wait_port(port)
    return server, port


def _origin_app(
    name: str, workspaces: dict[str, str], application_id: str, origin: dict[str, str]
) -> FastAPI:
    """A SaaS origin using the SDK middleware; the page names the workspace.

    ``origin["token"]`` is the proof-of-control token the operator publishes.
    """
    app = FastAPI()
    app.add_middleware(
        CustomDomainMiddleware,
        keys=dict(KEYS),
        application_id=application_id,
        workspace_lookup=workspaces.get,
        on_missing="reject",
    )

    @app.get("/", response_class=PlainTextResponse)
    def home(request: Request):
        assertion = request.state.custom_domain
        return f"{name}: {workspaces.get(assertion.reference, 'unknown workspace')}"

    @app.get(WELL_KNOWN_PATH, response_class=PlainTextResponse)
    def origin_verification():
        return origin.get("token", "")

    return app


def https_get(
    host: str, port: int, path: str, ca_file: str, source: str | None = None
) -> tuple[int, str]:
    """GET through the edge with ``host`` as SNI and Host header, trusting ``ca_file``.

    ``source`` is the client address to connect from (any 127.0.0.0/8 address
    works on Linux), so the edge sees a client other than 127.0.0.1.
    """
    context = ssl.create_default_context(cafile=ca_file)
    sock = socket.create_connection(
        ("127.0.0.1", port), timeout=15, source_address=(source, 0) if source else None
    )
    tls = context.wrap_socket(sock, server_hostname=host)
    try:
        connection = http.client.HTTPConnection(host, port, timeout=15)
        connection.sock = tls
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        tls.close()


def rejected(host: str, port: int, ca_file: str) -> bool:
    """A hostname is rejected when the edge refuses the handshake or answers without content."""
    try:
        status, body = https_get(host, port, "/", ca_file)
    except (ssl.SSLError, OSError):
        return True
    return status in (403, 404) and "workspace" not in body


def test_two_applications_serve_the_right_workspaces_over_https(
    session, session_factory, make_application, monkeypatch, tmp_path
):
    # --- operator: applications, credentials and proof-of-control of the origins ---
    bc = make_application("bettercollected", cname_target="bc.edge.localtest.me")
    sample = make_application("sample", cname_target="sample.edge.localtest.me")
    _, bc_secret = issue_credential(session, bc, label="e2e")
    _, sample_secret = issue_credential(session, sample, label="e2e")
    session.commit()
    bc_origin: dict[str, str] = {}
    sample_origin: dict[str, str] = {}
    bc_server, bc_port = _serve(
        _origin_app(
            "bettercollected",
            {"ws_alpha": "Alpha workspace", "ws_beta": "Beta workspace"},
            str(bc.id),
            bc_origin,
        )
    )
    sample_server, sample_port = _serve(
        _origin_app("sample", {"ws_one": "Sample workspace"}, str(sample.id), sample_origin)
    )
    for application, port, published in (
        (bc, bc_port, bc_origin),
        (sample, sample_port, sample_origin),
    ):
        origin = register_origin(session, application, host="localhost", scheme="http", port=port)
        session.commit()
        # Verification fails until the application publishes the token ...
        with pytest.raises(OriginVerificationFailed) as failure:
            verify_origin(session, origin, allow_private=True)
        assert failure.value.code == "token_mismatch"
        # ... and passes once it does, through the SDK middleware in reject mode.
        published["token"] = origin.verification_token
        verify_origin(session, origin, allow_private=True)
        activate_origin(session, origin)
    session.commit()

    # --- management API served for the SDK clients and for Caddy (ask + assert) ---
    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")  # the origins listen on loopback
    api = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    api.dependency_overrides[get_session] = override
    # As in entrypoint.sh: the API must judge the edge by the connecting
    # address, not by the X-Forwarded-For Caddy adds for the browser.
    api_server, api_port = _serve(api, proxy_headers=False)

    # --- the applications register their customers' hostnames through the SDK ---
    api_url = f"http://127.0.0.1:{api_port}"
    bc_client = Client(api_url, bc_secret)
    sample_client = Client(api_url, sample_secret)
    alpha = bc_client.create_domain(
        "alpha.customer.example", "ws_alpha", idempotency_key="ws_alpha-alpha"
    )
    beta = bc_client.create_domain("beta.customer.example", "ws_beta")
    one = sample_client.create_domain("one.other-customer.example", "ws_one")
    assert {d.status for d in (alpha, beta, one)} == {"pending_dns"}
    assert (
        alpha.id
        == bc_client.create_domain(
            "alpha.customer.example", "ws_alpha", idempotency_key="ws_alpha-alpha"
        ).id
    ), "an idempotent retry returns the same domain"

    # --- cross-application boundary: one tenant cannot see or touch another's domains ---
    with pytest.raises(ConflictError):
        sample_client.create_domain("alpha.customer.example", "ws_other")
    with pytest.raises(NotFoundError):
        sample_client.get_domain(alpha.id)
    with pytest.raises(NotFoundError):
        sample_client.delete_domain(alpha.id)
    with pytest.raises(NotFoundError):
        sample_client.request_recheck(alpha.id)
    assert {d.hostname for d in sample_client.list_domains().items} == {
        "one.other-customer.example"
    }
    assert {d.hostname for d in bc_client.list_domains().items} == {
        "alpha.customer.example",
        "beta.customer.example",
    }
    with pytest.raises(AuthenticationError):
        Client(api_url, "cd_not_a_real_credential_000000000000").list_domains()

    https_port = _free_port()
    http_port = _free_port()
    admin_port = _free_port()
    data_home = tmp_path / "caddy-data"
    ca_file = data_home / "caddy" / "pki" / "authorities" / "local" / "root.crt"
    settings = EdgeSettings(
        admin_url=f"http://127.0.0.1:{admin_port}",
        https_port=https_port,
        http_port=http_port,
        tls_issuer="internal",
        ask_url=f"http://127.0.0.1:{api_port}/internal/tls/ask",
        assert_upstream=f"127.0.0.1:{api_port}",
        assertion_keys=KEYS,
        edge_token=EDGE_TOKEN,
        ask_trusted_hosts=("127.0.0.1", "::1"),
        probe_address=f"127.0.0.1:{https_port}",
        probe_ca_file=str(ca_file),
        reconcile_enabled=True,
        legacy_api_enabled=False,
    )
    api.state.edge_settings = settings

    # --- real Caddy from the bootstrap; the reconciler applies the apps subtree ---
    bootstrap = build_bootstrap(settings)
    config_file = tmp_path / "bootstrap.json"
    config_file.write_text(json.dumps(bootstrap))
    caddy_log = open(tmp_path / "caddy.log", "wb")  # noqa: SIM115
    caddy = subprocess.Popen(
        ["caddy", "run", "--config", str(config_file)],
        stdout=subprocess.DEVNULL,
        stderr=caddy_log,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(data_home),
            "XDG_CONFIG_HOME": str(tmp_path),
        },
    )
    try:
        assert _wait_port(admin_port), (tmp_path / "caddy.log").read_text()[-2000:]
        reconciler = Reconciler(session_factory, CaddyClient(settings.admin_url), settings)
        assert reconciler.run_once().ok

        # --- DNS: the customers publish the records the API handed back ---
        resolver = FakeResolver()
        for domain in (alpha, beta, one):
            records = {r.purpose: r for r in domain.dns_records}
            resolver.txt_records[records["ownership"].name] = [records["ownership"].value]
            resolver.cnames[records["routing"].name] = records["routing"].value

        # --- the worker drives the domains to ready through the real edge ---
        prober = SystemEdgeProber(settings)
        deadline = time.time() + 60
        while time.time() < deadline:
            if not ca_file.exists():
                time.sleep(0.2)
                continue
            result = run_due_checks(
                session_factory,
                resolver,
                prober=prober,
                settings=settings,
                on_status_change=reconciler.run_once,
            )
            if result.status_changes:
                reconciler.run_once()
            session.commit()
            session.expire_all()
            if all(
                client.get_domain(d.id).status == "ready"
                for client, d in ((bc_client, alpha), (bc_client, beta), (sample_client, one))
            ):
                break
            time.sleep(0.5)
        caddy_log.flush()
        for client, d in ((bc_client, alpha), (bc_client, beta), (sample_client, one)):
            fresh = client.get_domain(d.id)
            assert fresh.status == "ready", (
                fresh.hostname,
                [(c.type, c.status, c.error_code, c.message) for c in fresh.checks],
            )
            assert {c.status for c in fresh.checks} == {"passing"}

        # --- correct HTTPS content per workspace, from the real edge ---
        assert https_get("alpha.customer.example", https_port, "/", str(ca_file)) == (
            200,
            "bettercollected: Alpha workspace",
        )
        assert https_get("beta.customer.example", https_port, "/", str(ca_file)) == (
            200,
            "bettercollected: Beta workspace",
        )
        assert https_get("one.other-customer.example", https_port, "/", str(ca_file)) == (
            200,
            "sample: Sample workspace",
        )

        # --- a client from another address is served: Caddy forwards that
        # address in X-Forwarded-For on the assert subrequest, and the API must
        # still judge the edge by the address that connects to it ---
        assert https_get(
            "alpha.customer.example", https_port, "/", str(ca_file), source="127.0.0.2"
        ) == (200, "bettercollected: Alpha workspace")

        # --- wrong host: no certificate is issued for an unknown name ---
        assert rejected("nobody.customer.example", https_port, str(ca_file))

        # --- recheck from the application: the checks re-run through the real edge ---
        assert bc_client.request_recheck(alpha.id).status == "ready"
        run_due_checks(session_factory, resolver, prober=prober, settings=settings)
        session.commit()
        assert bc_client.get_domain(alpha.id).status == "ready"

        # --- stale claim (operator re-issue): the domain leaves service until re-verified ---
        reissue_claim(session, bc, uuid.UUID(beta.id))
        session.commit()
        reconciler.run_once()
        assert rejected("beta.customer.example", https_port, str(ca_file))
        assert bc_client.get_domain(beta.id).status == "pending_dns"

        # --- deletion from the application: refused on the next request ---
        assert bc_client.delete_domain(alpha.id).status == "deleting"
        with pytest.raises(NotFoundError):
            bc_client.get_domain(alpha.id)
        assert bc_client.get_domain(alpha.id, include_deleted=True).status == "deleting"
        assert rejected("alpha.customer.example", https_port, str(ca_file))
        assert https_get("one.other-customer.example", https_port, "/", str(ca_file)) == (
            200,
            "sample: Sample workspace",
        )
    finally:
        caddy.terminate()
        caddy.wait(timeout=10)
        for server in (api_server, bc_server, sample_server):
            server.should_exit = True
