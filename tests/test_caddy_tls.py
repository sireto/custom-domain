"""Real Caddy: on-demand issuance gated by the ask endpoint, readiness through Caddy."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time

import pytest
import uvicorn

from app.db.session import get_session
from app.edge.caddy_client import CaddyClient
from app.edge.config import build_bootstrap
from app.edge.probe import EdgeProbeFailed, probe_edge
from app.edge.reconcile import Reconciler
from app.edge.settings import EdgeSettings
from app.main import create_app
from app.models import CheckStatus, DomainStatus
from app.services.domains import claim_domain, mark_claim_verified, transition_status
from app.services.edge_checks import SystemEdgeProber, certificate_outcome
from tests.caddy_support import caddy_required, free_port, wait_port

pytestmark = caddy_required()


def test_caddy_issues_only_for_authorized_hostnames_and_readiness_probe_passes(
    session, session_factory, make_application, monkeypatch, tmp_path
):
    acme = make_application("acme")
    authorized = claim_domain(session, acme, "ok.customer.example", "ws_1")
    mark_claim_verified(session, authorized)
    transition_status(session, authorized, DomainStatus.PROVISIONING)
    pending = claim_domain(session, acme, "pending.customer.example", "ws_2")
    session.commit()

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED"):
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
    api_port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(api, host="127.0.0.1", port=api_port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    assert wait_port(api_port)

    https_port, http_port, admin_port = free_port(), free_port(), free_port()
    data_home = tmp_path / "data"
    ca_file = data_home / "caddy" / "pki" / "authorities" / "local" / "root.crt"
    settings = EdgeSettings(
        admin_url=f"http://127.0.0.1:{admin_port}",
        https_port=https_port,
        http_port=http_port,
        tls_issuer="internal",
        ask_url=f"http://127.0.0.1:{api_port}/internal/tls/ask",
        ask_trusted_hosts=("127.0.0.1", "::1"),
        probe_address=f"127.0.0.1:{https_port}",
        probe_ca_file=str(ca_file),
        reconcile_enabled=True,
        legacy_api_enabled=False,
    )
    api.state.edge_settings = settings
    config_file = tmp_path / "bootstrap.json"
    config_file.write_text(json.dumps(build_bootstrap(settings)))
    log = open(tmp_path / "caddy.log", "wb")  # noqa: SIM115
    caddy = subprocess.Popen(
        ["caddy", "run", "--config", str(config_file)],
        stdout=subprocess.DEVNULL,
        stderr=log,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(data_home),
            "XDG_CONFIG_HOME": str(tmp_path),
        },
    )
    try:
        assert wait_port(admin_port), (tmp_path / "caddy.log").read_text()[-2000:]
        assert Reconciler(session_factory, CaddyClient(settings.admin_url), settings).run_once().ok
        deadline = time.time() + 30
        while not ca_file.exists() and time.time() < deadline:
            time.sleep(0.2)
        assert ca_file.exists()

        # Authorized: Caddy asks, the API allows, a certificate is issued on this first handshake.
        probe = probe_edge(
            "ok.customer.example",
            address="127.0.0.1",
            port=https_port,
            ca_file=str(ca_file),
            timeout=20,
        )
        assert probe.status == 204 and probe.edge_header == "1"
        outcome = certificate_outcome(SystemEdgeProber(settings), authorized, settings)
        assert outcome.status == CheckStatus.PASSING, outcome

        # Not authorized (claim pending) and unknown: the ask endpoint denies, no certificate.
        for host in ("pending.customer.example", "nobody.customer.example"):
            with pytest.raises((EdgeProbeFailed, ssl.SSLError, OSError)) as info:
                probe_edge(
                    host, address="127.0.0.1", port=https_port, ca_file=str(ca_file), timeout=20
                )
            if isinstance(info.value, EdgeProbeFailed):
                assert info.value.code in ("tls_handshake_failed", "connection_failed"), (
                    info.value.code
                )
        assert pending.status == DomainStatus.PENDING_DNS
    finally:
        caddy.terminate()
        caddy.wait(timeout=10)
        log.close()
        server.should_exit = True
