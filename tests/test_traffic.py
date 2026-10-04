"""Per-application traffic counted at the edge (#66)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cli
from app.edge.caddy_client import CaddyClient
from app.edge.config import build_apps, build_bootstrap
from app.edge.gateway import ConfigRejected, create_gateway_app, validate_apps
from app.edge.reconcile import Reconciler
from app.edge.traffic import parse_totals, render_totals
from app.models import (
    ApplicationTraffic,
    CheckStatus,
    CheckType,
    DomainStatus,
    EdgeTrafficCounter,
)
from app.services import traffic
from app.services.domains import (
    claim_domain,
    delete_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)
from app.services.errors import InvalidApplication
from tests.caddy_support import free_port, wait_port
from tests.test_hardening import ALLOWED, SETTINGS, _ready_domain


def _another_domain(session, application, hostname):
    """A second ready domain for an application that already has its origin."""
    domain = claim_domain(session, application, hostname, "ws_2")
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    return domain


NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

# Trimmed (and labels shortened) from Caddy 2.11's /metrics with per_host on.
CADDY_METRICS = """\
# HELP caddy_http_requests_total Counter of HTTP(S) requests made.
# TYPE caddy_http_requests_total counter
caddy_http_requests_total{handler="headers",host="a.example",server="edge"} 2
caddy_http_requests_total{handler="rate_limit",host="a.example",server="edge"} 3
caddy_http_requests_total{handler="headers",host="b.example",server="edge"} 1
caddy_http_requests_total{handler="static_response",host="_other",server="edge"} 7
caddy_http_requests_total{handler="static_response",host="a.example",server="assert"} 2
# HELP caddy_http_response_size_bytes Size of the returned response.
# TYPE caddy_http_response_size_bytes histogram
caddy_http_response_size_bytes_bucket{code="200",host="a.example",server="edge",le="+Inf"} 2
caddy_http_response_size_bytes_sum{code="200",handler="headers",host="a.example",server="edge"} 20
caddy_http_response_size_bytes_count{code="200",handler="headers",host="a.example",server="edge"} 2
caddy_http_response_size_bytes_sum{code="429",handler="rate_limit",host="a.example",server="edge"} 5
caddy_http_response_size_bytes_sum{code="200",handler="headers",host="b.example",server="edge"} 10
caddy_http_response_size_bytes_sum{code="404",handler="static",host="_other",server="edge"} 0
# HELP process_start_time_seconds Start time of the process.
# TYPE process_start_time_seconds gauge
process_start_time_seconds 1.79e+09
"""


def test_totals_sum_every_handler_and_code_per_edge_hostname():
    totals = parse_totals(CADDY_METRICS)
    assert totals == {"a.example": (5, 25), "b.example": (1, 10)}
    assert parse_totals(render_totals(totals)) == totals


def test_the_edge_counts_per_hostname_and_the_gateway_requires_it(session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme)
    apps = build_apps(session, SETTINGS)
    assert apps["http"]["metrics"] == {"per_host": True}
    validate_apps(apps, SETTINGS, ALLOWED)
    for change in (
        lambda a: a["http"].pop("metrics"),
        lambda a: a["http"].__setitem__("metrics", {"per_host": False}),
        lambda a: a["http"]["metrics"].__setitem__("extra", True),
    ):
        broken = copy.deepcopy(apps)
        change(broken)
        with pytest.raises(ConfigRejected):
            validate_apps(broken, SETTINGS, ALLOWED)


def test_the_gateway_passes_on_only_the_totals():
    def fake_caddy(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/metrics")
        return httpx.Response(200, text=CADDY_METRICS)

    app = create_gateway_app(
        SETTINGS,
        caddy_admin_url="http://caddy",
        origins_provider=lambda: ALLOWED,
        transport=httpx.MockTransport(fake_caddy),
    )
    body = TestClient(app).get("/metrics").text
    assert parse_totals(body) == {"a.example": (5, 25), "b.example": (1, 10)}
    assert "process_start_time" not in body and "_other" not in body and "assert" not in body

    down = create_gateway_app(
        SETTINGS,
        caddy_admin_url="http://caddy",
        origins_provider=lambda: ALLOWED,
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert TestClient(down).get("/metrics").status_code == 502

    def unreachable(request):
        raise httpx.ConnectError("refused")

    gone = create_gateway_app(
        SETTINGS,
        caddy_admin_url="http://caddy",
        origins_provider=lambda: ALLOWED,
        transport=httpx.MockTransport(unreachable),
    )
    assert TestClient(gone).get("/metrics").status_code == 502


def _day(session, application, day=NOW.date()):
    row = session.get(ApplicationTraffic, (application.id, day))
    return (row.requests, row.response_bytes) if row else None


def test_increases_go_to_the_hostnames_owner(session, make_application):
    acme, globex = make_application("acme"), make_application("globex")
    _ready_domain(session, acme, "a.customer.example")
    _ready_domain(session, globex, "g.customer.example")

    traffic.record_totals(
        session,
        {"a.customer.example": (5, 50), "g.customer.example": (1, 10), "stray.example": (9, 9)},
        now=NOW,
    )
    assert _day(session, acme) == (5, 50) and _day(session, globex) == (1, 10)
    later = NOW + timedelta(minutes=1)
    traffic.record_totals(
        session, {"A.customer.example": (8, 80), "g.customer.example": (1, 10)}, now=later
    )
    assert _day(session, acme) == (8, 80) and _day(session, globex) == (1, 10)


def test_counters_restarting_are_counted_from_zero(session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme, "a.customer.example")
    traffic.record_totals(session, {"a.customer.example": (100, 1000)}, now=NOW)
    # Caddy restarted: the total went down.
    traffic.record_totals(session, {"a.customer.example": (3, 30)}, now=NOW)
    assert _day(session, acme) == (103, 1030)
    # A new configuration restarted the counters (the reconciler says so).
    traffic.reset_baselines(session)
    traffic.record_totals(session, {"a.customer.example": (50, 500)}, now=NOW)
    assert _day(session, acme) == (153, 1530)


def test_a_deleted_domain_counts_for_nobody(session, make_application):
    acme = make_application("acme")
    domain = _ready_domain(session, acme, "a.customer.example")
    traffic.record_totals(session, {"a.customer.example": (1, 1)}, now=NOW)
    delete_domain(session, acme, domain.id)
    session.commit()
    traffic.record_totals(session, {"a.customer.example": (5, 5)}, now=NOW)
    assert _day(session, acme) == (1, 1)


def test_days_roll_over_and_old_rows_expire(session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme, "a.customer.example")
    old = NOW - timedelta(days=traffic.TRAFFIC_RETENTION_DAYS)
    traffic.record_totals(session, {"a.customer.example": (1, 1)}, now=old)
    traffic.record_totals(session, {"a.customer.example": (2, 2)}, now=NOW - timedelta(days=1))
    session.add(
        EdgeTrafficCounter(hostname="gone.example", requests=1, response_bytes=1, read_at=old)
    )
    traffic.record_totals(session, {"a.customer.example": (4, 4)}, now=NOW)
    assert _day(session, acme, old.date()) is None
    assert _day(session, acme, NOW.date() - timedelta(days=1)) == (1, 1)
    assert _day(session, acme) == (2, 2)
    assert session.get(EdgeTrafficCounter, "gone.example") is None

    days = traffic.application_traffic(session, acme, days=3, now=NOW)
    assert [(d.day, d.requests) for d in days] == [
        (date(2026, 10, 2), 0),
        (date(2026, 10, 3), 1),
        (date(2026, 10, 4), 2),
    ]
    for bad in (0, traffic.TRAFFIC_RETENTION_DAYS + 1):
        with pytest.raises(InvalidApplication):
            traffic.application_traffic(session, acme, days=bad)


def test_human_bytes():
    assert traffic.human_bytes(999) == "999 B"
    assert traffic.human_bytes(1500) == "1.5 KB"
    assert traffic.human_bytes(10 * 10**9) == "10.0 GB"
    assert traffic.human_bytes(3 * 10**15) == "3000.0 TB"


class TrafficCaddy:
    """Fake admin endpoint: Caddy restarts its counters on every new config."""

    def __init__(self):
        self.running = build_bootstrap(SETTINGS)
        self.totals: dict[str, tuple[int, int]] = {}
        self.fail_traffic: Exception | None = None

    def get_config(self):
        return self.running

    def load_config(self, config):
        self.running = config

    def set_apps(self, apps):
        self.running = {**self.running, "apps": apps}
        self.totals = {}

    def traffic(self):
        if self.fail_traffic:
            raise self.fail_traffic
        return dict(self.totals)


def test_the_reconciler_reads_before_a_new_config_restarts_the_counters(
    session, session_factory, make_application
):
    acme = make_application("acme")
    _ready_domain(session, acme, "a.customer.example")
    caddy = TrafficCaddy()
    reconciler = Reconciler(session_factory, caddy, SETTINGS)
    assert reconciler.run_once().changed
    caddy.totals = {"a.customer.example": (10, 100)}
    assert not reconciler.run_once().changed
    caddy.totals = {"a.customer.example": (15, 150)}
    _another_domain(session, acme, "b.customer.example")  # the next run applies a new config
    assert reconciler.run_once().changed and caddy.totals == {}
    caddy.totals = {"a.customer.example": (2, 20)}
    reconciler.run_once()
    session.expire_all()
    today = datetime.now(UTC).date()
    assert _day(session, acme, today) == (17, 170)

    # Traffic that cannot be read never stops the edge from converging.
    caddy.fail_traffic = RuntimeError("boom")
    _another_domain(session, acme, "c.customer.example")
    assert reconciler.run_once().changed


def test_the_operator_api_reports_traffic(session_factory, session, monkeypatch):
    from tests.test_operator_api import AUTH, make_app

    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    base = "/operator/v1/applications"
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        client.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
        from app.services.applications import get_application_by_slug

        acme = get_application_by_slug(session, "acme")
        session.add(
            ApplicationTraffic(
                application_id=acme.id,
                day=datetime.now(UTC).date(),
                requests=7,
                response_bytes=700,
            )
        )
        session.commit()
        got = client.get(f"{base}/acme/traffic?days=7", headers=AUTH).json()
        assert got["application"] == "acme" and len(got["days"]) == 7
        assert got["days"][-1]["requests"] == 7 and got["response_bytes"] == 700
        assert client.get(f"{base}/acme/traffic", headers=AUTH).json()["requests"] == 7
        assert client.get(f"{base}/acme/traffic?days=0", headers=AUTH).status_code == 422
        assert client.get(f"{base}/nope/traffic", headers=AUTH).status_code == 404
        assert client.get(f"{base}/acme/traffic").status_code == 401


def test_the_cli_prints_traffic(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    create = ["application", "create", "--slug", "acme", "--name", "Acme"]
    assert cli.main([*create, "--cname-target", "edge.example.net"]) == 0
    capsys.readouterr()
    assert cli.main(["application", "traffic", "--application", "acme", "--days", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5 and lines[-1].split() == ["total", "0", "0", "B"]


def test_the_portal_shows_traffic(portal):
    from tests.test_portal import csrf_of, sign_in
    from tests.test_portal_manage import _create

    sign_in(portal)
    _create(portal, csrf_of(portal))
    page = portal.get("/portal/applications/acme/traffic")
    assert page.status_code == 200 and "Last 30 days" in page.text
    assert 'href="/portal/applications/acme/traffic"' in page.text


def test_real_caddy_counts_the_generated_config(session, make_application, tmp_path):
    """Caddy, loaded with what the reconciler builds, reports per-hostname totals."""
    caddy = shutil.which("caddy")
    if caddy is None:
        if os.environ.get("REQUIRE_CADDY"):
            pytest.fail("REQUIRE_CADDY is set but caddy is not installed")
        pytest.skip("caddy is not installed")
    acme = make_application("acme")
    _ready_domain(session, acme, "a.customer.example")
    _another_domain(session, acme, "b.customer.example")
    port, admin, upstream = free_port(), free_port(), free_port()
    settings = SETTINGS.__class__(
        **{
            **SETTINGS.__dict__,
            "disable_https": True,
            "https_port": port,
            "http_port": free_port(),
            "assert_upstream": f"127.0.0.1:{upstream}",
        }
    )
    apps = build_apps(session, settings)
    for route in apps["http"]["servers"]["edge"]["routes"]:
        if route.get("@id") == "app-acme":
            route["handle"][-1]["upstreams"] = [{"dial": f"127.0.0.1:{upstream}"}]
            route["handle"][-1].pop("transport", None)  # the stand-in origin is plain HTTP
    # One server answers both the assertion subrequest and the origin.
    apps["http"]["servers"]["upstream"] = {
        "listen": [f"127.0.0.1:{upstream}"],
        "routes": [{"handle": [{"handler": "static_response", "body": "0123456789"}]}],
    }
    config = {"admin": {"listen": f"127.0.0.1:{admin}"}, "apps": apps}
    path = tmp_path / "caddy.json"
    path.write_text(json.dumps(config))
    process = subprocess.Popen([caddy, "run", "--config", str(path)], stderr=subprocess.DEVNULL)
    try:
        assert wait_port(port)
        hosts = ("a.customer.example", "a.customer.example", "b.customer.example", "x.example")
        statuses = [
            httpx.get(f"http://127.0.0.1:{port}/", headers={"Host": host}).status_code
            for host in hosts
        ]
        assert statuses == [200, 200, 200, 404]
        totals = CaddyClient(f"http://127.0.0.1:{admin}").traffic()
        assert totals["a.customer.example"] == (2, 20) and totals["b.customer.example"] == (1, 10)
        assert set(totals) <= {"a.customer.example", "b.customer.example", "127.0.0.1"}
    finally:
        process.terminate()
        process.wait(timeout=10)
        time.sleep(0.1)
