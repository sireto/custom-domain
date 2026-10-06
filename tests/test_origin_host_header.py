"""Origin host-header mode (#75): the edge can send the origin's own name as Host."""

from __future__ import annotations

import copy
import uuid

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.edge.config import build_apps, origin_host_value
from app.edge.gateway import ConfigRejected, validate_apps
from app.models import OriginHostHeader
from app.services import applications as app_service
from app.services.errors import InvalidOrigin
from tests.test_hardening import ALLOWED, SETTINGS, _ready_domain


def _proxy(apps):
    routes = apps["http"]["servers"]["edge"]["routes"]
    return [r for r in routes if r["@id"].startswith("app-")][0]["handle"][-1]


def test_origin_mode_sends_the_origins_own_name(session, make_application):
    acme = make_application("acme")
    domain = _ready_domain(session, acme)
    customer = _proxy(build_apps(session, SETTINGS))["headers"]["request"]["set"]
    assert customer["Host"] == ["{http.request.host}"]
    origin = acme.serving_origin
    app_service.set_origin_host_header(session, origin, "origin")
    session.commit()
    sent = _proxy(build_apps(session, SETTINGS))["headers"]["request"]["set"]
    assert sent["Host"] == ["app.acme.example"]
    assert sent["X-Forwarded-Host"] == ["{http.request.host}"]
    assert domain.hostname == "forms.customer.example"
    assert origin_host_value("app.acme.example", 8443, True) == "app.acme.example:8443"
    assert origin_host_value("app.acme.example", 80, False) == "app.acme.example"


def test_the_gateway_accepts_only_the_customers_hostname_or_the_origins_name(
    session, make_application
):
    acme = make_application("acme")
    _ready_domain(session, acme)
    app_service.set_origin_host_header(session, acme.serving_origin, "origin")
    session.commit()
    apps = build_apps(session, SETTINGS)
    validate_apps(apps, SETTINGS, ALLOWED)
    for host in ("evil.example", "app.acme.example:8443", "{http.request.header.X-Target}"):
        broken = copy.deepcopy(apps)
        _proxy(broken)["headers"]["request"]["set"]["Host"] = [host]
        with pytest.raises(ConfigRejected):
            validate_apps(broken, SETTINGS, ALLOWED)
    broken = copy.deepcopy(apps)
    _proxy(broken)["headers"]["request"]["set"]["X-Forwarded-Host"] = ["evil.example"]
    with pytest.raises(ConfigRejected):
        validate_apps(broken, SETTINGS, ALLOWED)


def test_modes_are_validated(session, make_application):
    acme = make_application("acme")
    origin = app_service.register_origin(
        session, acme, host="app.acme.example", host_header="origin"
    )
    assert origin.host_header == OriginHostHeader.ORIGIN
    with pytest.raises(InvalidOrigin):
        app_service.register_origin(session, acme, host="b.acme.example", host_header="sni")
    with pytest.raises(InvalidOrigin):
        app_service.set_origin_host_header(session, origin, "both")
    plain = app_service.register_origin(session, acme, host="c.acme.example")
    assert plain.host_header == OriginHostHeader.CUSTOMER


def test_the_operator_api(session_factory, session, monkeypatch):
    from tests.test_operator_api import AUTH, make_app

    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    base = "/operator/v1/applications"
    with TestClient(app, client=("10.0.0.5", 1000)) as c:
        c.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
        c.post(base, json={"slug": "globex", "name": "Globex"}, headers=AUTH)
        created = c.post(
            f"{base}/acme/origins",
            json={"host": "app.acme.example", "host_header": "origin"},
            headers=AUTH,
        ).json()
        assert created["host_header"] == "origin"
        changed = c.patch(
            f"{base}/acme/origins/{created['id']}", json={"host_header": "customer"}, headers=AUTH
        )
        assert changed.status_code == 200 and changed.json()["host_header"] == "customer"
        bad = c.patch(
            f"{base}/acme/origins/{created['id']}", json={"host_header": "x"}, headers=AUTH
        )
        assert bad.status_code == 422, bad.text
        other = c.patch(
            f"{base}/globex/origins/{created['id']}", json={"host_header": "origin"}, headers=AUTH
        )
        assert other.status_code == 404, other.text
        missing = c.patch(
            f"{base}/acme/origins/{uuid.uuid4()}", json={"host_header": "origin"}, headers=AUTH
        )
        assert missing.status_code == 404, missing.text


def test_the_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    create = ["application", "create", "--slug", "acme", "--name", "Acme"]
    assert cli.main([*create, "--cname-target", "edge.example.net"]) == 0
    register = ["origin", "register", "--application", "acme", "--host", "app.acme.example"]
    assert cli.main([*register, "--host-header", "origin"]) == 0
    assert "X-Forwarded-Host" in capsys.readouterr().out
    switch = ["origin", "set-host-header", "--application", "acme", "--host", "app.acme.example"]
    assert cli.main([*switch, "--mode", "customer"]) == 0
    assert "the customer's hostname" in capsys.readouterr().out


def test_the_portal(portal):
    from tests.test_portal import csrf_of, sign_in
    from tests.test_portal_manage import _create

    sign_in(portal)
    csrf = csrf_of(portal)
    _create(portal, csrf)
    portal.post(
        "/portal/applications/acme/origins",
        data={"csrf": csrf, "host": "app.acme.example", "host_header": "origin"},
    )
    page = portal.get("/portal/applications/acme/origins").text
    assert "Origin&#39;s own name" in page or "Origin's own name" in page
    import re

    from app.db.session import get_session_factory  # noqa: F401

    origin_id = re.search(r"/origins/([0-9a-f-]{36})/host-header", page).group(1)
    done = portal.post(
        f"/portal/applications/acme/origins/{origin_id}/host-header",
        data={"csrf": csrf, "host_header": "customer"},
        follow_redirects=False,
    )
    assert done.status_code == 303 and "origin_host_header" in done.headers["location"]
    bad = portal.post(
        f"/portal/applications/acme/origins/{origin_id}/host-header",
        data={"csrf": csrf, "host_header": "sni"},
    )
    assert bad.status_code == 400
