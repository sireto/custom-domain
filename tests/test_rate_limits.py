"""Per-application request limits at the edge (#65)."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cli
from app.edge.config import build_apps, rate_limit_handler
from app.edge.gateway import ConfigRejected, validate_apps
from app.services import applications as app_service
from app.services.errors import InvalidApplication
from tests.caddy_support import free_port, wait_port
from tests.test_hardening import ALLOWED, SETTINGS, _ready_domain


def _app_route(apps):
    routes = apps["http"]["servers"]["edge"]["routes"]
    return [r for r in routes if r["@id"].startswith("app-")][0]


def test_no_limit_no_handler_and_a_limit_comes_first(session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme)
    assert len(_app_route(build_apps(session, SETTINGS))["handle"]) == 3

    app_service.set_rate_limits(session, acme, per_minute=600, per_second=100)
    session.commit()
    first = _app_route(build_apps(session, SETTINGS))["handle"][0]
    assert first == {
        "handler": "rate_limit",
        "rate_limits": {
            "app-acme-minute": {"key": "app-acme", "window": "1m", "max_events": 600},
            "app-acme-second": {"key": "app-acme", "window": "1s", "max_events": 100},
        },
    }
    app_service.set_rate_limits(session, acme, per_minute=None, per_second=5)
    session.commit()
    zones = _app_route(build_apps(session, SETTINGS))["handle"][0]["rate_limits"]
    assert list(zones) == ["app-acme-second"]


def test_the_gateway_accepts_only_the_reconcilers_rate_limit(session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme)
    app_service.set_rate_limits(session, acme, per_minute=600, per_second=100)
    session.commit()
    apps = build_apps(session, SETTINGS)
    validate_apps(apps, SETTINGS, ALLOWED)

    def zone(a, name="app-acme-minute"):
        return _app_route(a)["handle"][0]["rate_limits"][name]

    cases = {
        # Keyed on a request value, a client could spread over keys and escape.
        "client key": lambda a: zone(a).__setitem__("key", "{http.request.remote.host}"),
        "another app's key": lambda a: zone(a).__setitem__("key", "app-globex"),
        "a day window": lambda a: zone(a).__setitem__("window", "24h"),
        "zero": lambda a: zone(a).__setitem__("max_events", 0),
        "a string count": lambda a: zone(a).__setitem__("max_events", "600"),
        "huge": lambda a: zone(a).__setitem__("max_events", 10**9),
        "extra zone option": lambda a: zone(a).__setitem__("distributed", {}),
        "foreign zone name": lambda a: _app_route(a)["handle"][0]["rate_limits"].__setitem__(
            "app-globex-minute", {"key": "app-acme", "window": "1m", "max_events": 5}
        ),
        "no zones": lambda a: _app_route(a)["handle"][0].__setitem__("rate_limits", {}),
        "extra handler key": lambda a: _app_route(a)["handle"][0].__setitem__("jitter", 0.5),
        "something else first": lambda a: _app_route(a)["handle"].__setitem__(
            0, {"handler": "static_response", "body": "x"}
        ),
    }
    for name, change in cases.items():
        broken = copy.deepcopy(apps)
        change(broken)
        with pytest.raises(ConfigRejected):
            validate_apps(broken, SETTINGS, ALLOWED)
        assert name


def test_limits_are_validated(session, make_application):
    acme = make_application("acme")
    for bad in (0, -1, 1_000_001):
        with pytest.raises(InvalidApplication):
            app_service.set_rate_limits(session, acme, per_minute=bad, per_second=None)
    assert rate_limit_handler("app-acme", None, None) is None


def test_the_operator_api_sets_and_lifts_limits(session_factory, session, monkeypatch):
    from tests.test_operator_api import AUTH, make_app

    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    base = "/operator/v1/applications"
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        client.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
        set_both = {"rate_limit_per_minute": 600, "rate_limit_per_second": 100}
        got = client.patch(f"{base}/acme", json=set_both, headers=AUTH).json()
        assert (got["rate_limit_per_minute"], got["rate_limit_per_second"]) == (600, 100)
        # Omitted fields stay; null lifts.
        got = client.patch(f"{base}/acme", json={"rate_limit_per_second": None}, headers=AUTH)
        assert got.json()["rate_limit_per_minute"] == 600
        assert got.json()["rate_limit_per_second"] is None
        bad = client.patch(f"{base}/acme", json={"rate_limit_per_minute": 0}, headers=AUTH)
        assert bad.status_code == 422


def test_the_cli_sets_limits(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    create = ["application", "create", "--slug", "acme", "--name", "Acme"]
    assert cli.main([*create, "--cname-target", "edge.example.net"]) == 0
    capsys.readouterr()
    rate = ["application", "set-rate-limit", "--application", "acme"]
    assert cli.main([*rate, "--per-minute", "600", "--per-second", "100"]) == 0
    assert "600 a minute and 100 a second" in capsys.readouterr().out
    assert cli.main(rate) == 0
    assert "no limit" in capsys.readouterr().out


def test_the_portal_sets_limits(portal):
    from tests.test_portal import csrf_of, sign_in
    from tests.test_portal_manage import _create

    sign_in(portal)
    csrf = csrf_of(portal)
    _create(portal, csrf)
    assert "Request limits" in portal.get("/portal/applications/acme/settings").text
    saved = portal.post(
        "/portal/applications/acme/rate-limit",
        data={"csrf": csrf, "per_minute": "600", "per_second": ""},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and "ok=rate_limit" in saved.headers["location"]
    page = portal.get("/portal/applications/acme/settings").text
    assert 'value="600"' in page
    bad = portal.post(
        "/portal/applications/acme/rate-limit",
        data={"csrf": csrf, "per_minute": "lots", "per_second": ""},
    )
    assert bad.status_code == 400


def _caddy_with_rate_limit() -> str | None:
    caddy = shutil.which("caddy")
    if caddy is None:
        return None
    modules = subprocess.run([caddy, "list-modules"], capture_output=True, text=True).stdout
    return caddy if "http.handlers.rate_limit" in modules else None


def test_real_caddy_enforces_the_generated_limit(tmp_path):
    """The exact handler the reconciler builds, loaded by Caddy with the module."""
    import os

    caddy = _caddy_with_rate_limit()
    if caddy is None:
        if os.environ.get("REQUIRE_CADDY"):
            pytest.fail("REQUIRE_CADDY is set but caddy lacks http.handlers.rate_limit")
        pytest.skip("caddy with the rate-limit module is not installed")
    port, admin = free_port(), free_port()
    handler = rate_limit_handler("app-acme", 3, 100)
    config = {
        "admin": {"listen": f"127.0.0.1:{admin}"},
        "apps": {
            "http": {
                "servers": {
                    "edge": {
                        "listen": [f"127.0.0.1:{port}"],
                        "automatic_https": {"disable": True},
                        "routes": [
                            {
                                "@id": "app-acme",
                                "match": [{"host": ["a.example", "b.example"]}],
                                "handle": [
                                    handler,
                                    {"handler": "static_response", "status_code": 200},
                                ],
                                "terminal": True,
                            }
                        ],
                    }
                }
            }
        },
    }
    path = tmp_path / "caddy.json"
    path.write_text(json.dumps(config))
    process = subprocess.Popen([caddy, "run", "--config", str(path)], stderr=subprocess.DEVNULL)
    try:
        assert wait_port(port)
        statuses = []
        for host in ("a.example", "b.example", "a.example", "b.example"):
            r = httpx.get(f"http://127.0.0.1:{port}/", headers={"Host": host})
            statuses.append((r.status_code, r.headers.get("retry-after")))
        # Both hostnames count against the one application.
        assert [s for s, _ in statuses] == [200, 200, 200, 429]
        assert statuses[-1][1] is not None and int(statuses[-1][1]) > 0
    finally:
        process.terminate()
        process.wait(timeout=10)
        time.sleep(0.1)
