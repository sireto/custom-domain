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

import pytest
import uvicorn
from custom_domain import CustomDomainMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.db.session import get_session
from app.dns.worker import run_due_checks
from app.edge.caddy_client import CaddyClient
from app.edge.config import build_bootstrap
from app.edge.reconcile import Reconciler
from app.edge.settings import EdgeSettings
from app.main import create_app
from app.models import DomainStatus
from app.services.applications import (
    activate_origin,
    issue_credential,
    record_origin_verification,
    register_origin,
)
from app.services.domains import claim_domain, delete_domain, get_domain, reissue_claim
from app.services.edge_checks import SystemEdgeProber
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


def _serve(app: FastAPI) -> tuple[uvicorn.Server, int]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    assert _wait_port(port)
    return server, port


def _origin_app(name: str, workspaces: dict[str, str], application_id: str) -> FastAPI:
    """A SaaS origin using the SDK middleware; the page names the workspace."""
    app = FastAPI()
    app.add_middleware(
        CustomDomainMiddleware,
        keys=dict(KEYS),
        application_id=application_id,
        on_missing="reject",
    )

    @app.get("/", response_class=PlainTextResponse)
    def home(request: Request):
        assertion = request.state.custom_domain
        return f"{name}: {workspaces.get(assertion.reference, 'unknown workspace')}"

    return app


def https_get(host: str, port: int, path: str, ca_file: str) -> tuple[int, str]:
    """GET through the edge with ``host`` as SNI and Host header, trusting ``ca_file``."""
    context = ssl.create_default_context(cafile=ca_file)
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
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
    # --- applications: BetterCollected-like with two workspaces, plus the sample SaaS ---
    bc = make_application("bettercollected", cname_target="bc.edge.localtest.me")
    sample = make_application("sample", cname_target="sample.edge.localtest.me")
    _, bc_secret = issue_credential(session, bc, label="e2e")
    session.commit()
    bc_server, bc_port = _serve(
        _origin_app(
            "bettercollected",
            {"ws_alpha": "Alpha workspace", "ws_beta": "Beta workspace"},
            str(bc.id),
        )
    )
    sample_server, sample_port = _serve(
        _origin_app("sample", {"ws_one": "Sample workspace"}, str(sample.id))
    )
    for application, port in ((bc, bc_port), (sample, sample_port)):
        origin = register_origin(session, application, host="localhost", scheme="http", port=port)
        record_origin_verification(session, origin, verified=True)
        activate_origin(session, origin)
    alpha = claim_domain(session, bc, "alpha.customer.example", "ws_alpha")
    beta = claim_domain(session, bc, "beta.customer.example", "ws_beta")
    one = claim_domain(session, sample, "one.other-customer.example", "ws_one")
    session.commit()

    # --- management API served for Caddy (ask + assert) ---
    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    api = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    api.dependency_overrides[get_session] = override
    api_server, api_port = _serve(api)

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

        # --- DNS: the customers publish the records (in-memory resolver) ---
        resolver = FakeResolver()
        for domain in (alpha, beta, one):
            claim = domain.active_claim
            resolver.txt_records[claim.txt_record_name] = [claim.txt_record_value]
            resolver.cnames[domain.hostname] = claim.cname_target

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
                get_domain(session, d.application, d.id).status == DomainStatus.READY
                for d in (alpha, beta, one)
            ):
                break
            time.sleep(0.5)
        caddy_log.flush()
        for d in (alpha, beta, one):
            fresh = get_domain(session, d.application, d.id)
            for c in fresh.checks:
                print(
                    "CHECK",
                    fresh.hostname,
                    c.check_type.value,
                    c.status.value,
                    c.error_code,
                    c.message,
                )
            assert fresh.status == DomainStatus.READY, (
                fresh.hostname,
                [
                    (c.check_type.value, c.status.value, c.error_code, c.message)
                    for c in fresh.checks
                ],
            )

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

        # --- wrong host: no certificate is issued for an unknown name ---
        assert rejected("nobody.customer.example", https_port, str(ca_file))

        # --- stale claim: re-issued instructions take the domain out of service ---
        reissue_claim(session, bc, beta.id)
        session.commit()
        reconciler.run_once()
        assert rejected("beta.customer.example", https_port, str(ca_file))

        # --- revoked domain: deletion is refused on the next request ---
        delete_domain(session, bc, alpha.id)
        session.commit()
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
