"""The operator API: applications, origins and credentials over HTTP."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_origin_verification import TokenServer

TOKEN = "operator-token-0123456789abcdef-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_app(session_factory, monkeypatch, token: str | None = TOKEN, **env: str):
    from app.db.session import get_session
    from app.main import create_app

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "false")
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")  # the test origin listens on loopback
    monkeypatch.setenv("EDGE_HOSTNAME", "edge.example.net")
    monkeypatch.setenv("EDGE_ASK_TRUSTED_HOSTS", "127.0.0.1")
    if token is None:
        monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OPERATOR_API_TOKEN", token)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    return app


@pytest.fixture
def operator(session_factory, session, monkeypatch):
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    with TestClient(app, client=("10.0.0.5", 1000)) as client:  # a private peer
        yield client


def test_it_is_off_without_a_token_or_with_a_short_one(session_factory, session, monkeypatch):
    for token in (None, "too-short"):
        app = make_app(session_factory, monkeypatch, token=token)
        with TestClient(app, client=("127.0.0.1", 1000)) as client:
            response = client.get("/operator/v1/applications", headers=AUTH)
            assert response.status_code == 404, token


def test_tokens_addresses_and_throttling(session_factory, session, monkeypatch):
    app = make_app(
        session_factory,
        monkeypatch,
        OPERATOR_ALLOWED_IPS="93.184.216.34",
        V1_AUTH_FAILURES_PER_MINUTE="3",
    )
    with TestClient(app, client=("10.0.0.5", 1000)) as private:
        assert private.get("/operator/v1/applications").status_code == 401
        wrong = {"Authorization": f"Bearer {TOKEN[:-1]}x"}
        assert private.get("/operator/v1/applications", headers=wrong).status_code == 401
        assert private.get("/operator/v1/applications", headers=AUTH).status_code == 200
    # A public address connecting directly is refused before the token is looked at.
    public = TestClient(app, client=("8.8.8.8", 1000))
    refused = public.get("/operator/v1/applications", headers=AUTH)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "address_not_allowed"
    # Through the edge (a trusted peer), the forwarded address decides.
    edge = TestClient(app, client=("127.0.0.1", 1000))
    allowed = {**AUTH, "X-Forwarded-For": "93.184.216.34"}
    assert edge.get("/operator/v1/applications", headers=allowed).status_code == 200
    other = {**AUTH, "X-Forwarded-For": "8.8.8.8"}
    assert edge.get("/operator/v1/applications", headers=other).status_code == 403
    # Wrong tokens from an allowed public client are throttled, before any comparison.
    bad = {"Authorization": "Bearer wrong", "X-Forwarded-For": "93.184.216.34"}
    for _ in range(3):
        assert edge.get("/operator/v1/applications", headers=bad).status_code == 401
    limited = edge.get("/operator/v1/applications", headers=allowed)
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_an_application_is_provisioned_end_to_end(operator):
    base = "/operator/v1/applications"
    created = operator.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
    assert created.status_code == 201, created.text
    assert created.json()["cname_target"] == "edge.example.net"  # EDGE_HOSTNAME by default
    again = operator.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
    assert (
        again.status_code == 409 and again.json()["error"]["code"] == "application_already_exists"
    )
    bad = operator.post(base, json={"slug": "Bad Slug", "name": "x"}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_application"
    assert operator.get(f"{base}/nope", headers=AUTH).status_code == 404

    patched = operator.patch(
        f"{base}/acme",
        json={"name": "Acme Forms", "workspace_probe": False, "cname_target": "edge2.example.net"},
        headers=AUTH,
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Acme Forms" and patched.json()["workspace_probe"] is False
    assert patched.json()["cname_target"] == "edge2.example.net"
    assert [a["slug"] for a in operator.get(base, headers=AUTH).json()] == ["acme"]

    # Origin: register, fail, verify against a real listener and activate.
    server = TokenServer()
    try:
        origin = operator.post(
            f"{base}/acme/origins",
            json={"host": "localhost", "scheme": "http", "port": server.port},
            headers=AUTH,
        )
        assert origin.status_code == 201
        body = origin.json()
        assert body["status"] == "pending" and body["verification_url"].endswith(
            "/.well-known/custom-domain-origin-verification"
        )
        server.body = b"wrong"
        failed = operator.post(f"{base}/acme/origins/{body['id']}/verify", headers=AUTH)
        assert failed.status_code == 422
        assert failed.json()["error"]["code"] == "origin_verification_failed"
        server.body = body["verification_token"].encode()
        verified = operator.post(f"{base}/acme/origins/{body['id']}/verify", headers=AUTH)
        assert verified.status_code == 200 and verified.json()["active"] is True
    finally:
        server.close()
    in_use = operator.delete(f"{base}/acme/origins/{body['id']}", headers=AUTH)
    assert in_use.status_code == 409 and in_use.json()["error"]["code"] == "origin_in_use"

    # Credential: shown once, and it works on the v1 API.
    issued = operator.post(f"{base}/acme/credentials", json={"label": "backend"}, headers=AUTH)
    assert issued.status_code == 201
    secret, credential_id = issued.json()["secret"], issued.json()["id"]
    assert secret.startswith("cd_")
    listed = operator.get(f"{base}/acme/credentials", headers=AUTH).json()
    assert [c["id"] for c in listed] == [credential_id] and "secret" not in listed[0]
    v1 = operator.get("/v1/domains", headers={"Authorization": f"Bearer {secret}"})
    assert v1.status_code == 200
    rotated = operator.post(
        f"{base}/acme/credentials/{credential_id}/rotate", json={"grace_hours": 0}, headers=AUTH
    )
    assert rotated.status_code == 201 and rotated.json()["secret"] != secret
    early = operator.delete(f"{base}/acme/credentials/{rotated.json()['id']}", headers=AUTH)
    assert early.status_code == 409 and early.json()["error"]["code"] == "credential_in_use"
    revoked = operator.post(f"{base}/acme/credentials/{rotated.json()['id']}/revoke", headers=AUTH)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert (
        operator.delete(f"{base}/acme/credentials/{rotated.json()['id']}", headers=AUTH).status_code
        == 204
    )

    # Delete: the slug must be repeated, and live domains must be confirmed.
    operator.post(
        "/v1/domains",
        json={"hostname": "forms.customer.example", "reference": "ws_1"},
        headers={"Authorization": f"Bearer {rotated.json()['secret']}"},
    )
    wrong = operator.delete(f"{base}/acme", params={"confirm": "acm"}, headers=AUTH)
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "confirmation_mismatch"
    deleted = operator.delete(
        f"{base}/acme", params={"confirm": "acme", "delete_domains": "true"}, headers=AUTH
    )
    assert deleted.status_code == 200, deleted.text
    assert operator.get(f"{base}/acme", headers=AUTH).status_code == 404


def test_the_gateway_accepts_only_the_reconcilers_operator_routes(session, make_application):
    import copy

    from app.edge.config import build_apps
    from app.edge.gateway import ConfigRejected, EdgeFacts, validate_apps
    from tests.test_hardening import ALLOWED, SETTINGS, _ready_domain

    acme = make_application("acme", cname_target="acme.edge.example.net")
    _ready_domain(session, acme)
    enabled = SETTINGS.__class__(
        **{
            **SETTINGS.__dict__,
            "operator_allowed_ips": ("93.184.216.34",),
            "operator_api_enabled": True,
        }
    )
    apps = build_apps(session, enabled)
    routes = apps["http"]["servers"]["edge"]["routes"]
    assert [r["@id"] for r in routes[:3]] == ["edge-health", "operator", "operator-denied"]
    assert routes[1]["match"][0]["path"] == ["/operator", "/operator/*"]
    assert routes[1]["match"][0]["remote_ip"]["ranges"] == ["93.184.216.34/32"]
    facts = EdgeFacts(
        upstreams=ALLOWED,
        edge_names=frozenset({"acme.edge.example.net"}),
        operator_ranges=("93.184.216.34/32",),
    )
    validate_apps(apps, SETTINGS, facts)
    with pytest.raises(ConfigRejected):  # the API states no operator allowlist
        validate_apps(apps, SETTINGS, EdgeFacts(upstreams=ALLOWED, edge_names=facts.edge_names))
    for mutate in (
        lambda r: r[1]["match"][0]["remote_ip"]["ranges"].append("0.0.0.0/0"),
        lambda r: r[1]["match"][0].__setitem__("path", ["/*"]),
        lambda r: r[1]["handle"][0]["upstreams"].__setitem__(0, {"dial": "evil:1"}),
        lambda r: r.pop(2),  # without the 403 for everyone else
    ):
        bad = copy.deepcopy(apps)
        mutate(bad["http"]["servers"]["edge"]["routes"])
        with pytest.raises(ConfigRejected):
            validate_apps(bad, SETTINGS, facts)
    # An allowlist without a token emits nothing, so the facts and the routes agree.
    no_token = SETTINGS.__class__(**{**enabled.__dict__, "operator_api_enabled": False})
    ids = [r["@id"] for r in build_apps(session, no_token)["http"]["servers"]["edge"]["routes"]]
    assert "operator" not in ids and no_token.operator_ranges() == []


def test_every_operator_call_is_audited_without_secrets(operator, caplog):
    import logging

    from app.observability import install_log_redaction

    install_log_redaction()  # as in production
    base = "/operator/v1/applications"
    with caplog.at_level(logging.INFO, logger="app.operator.audit"):
        operator.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
        issued = operator.post(f"{base}/acme/credentials", json={"label": "backend"}, headers=AUTH)
        secret, credential_id = issued.json()["secret"], issued.json()["id"]
        operator.get(base, headers={"Authorization": "Bearer wrong-token"})
        operator.delete(f"{base}/acme", params={"confirm": "acme"}, headers=AUTH)
    lines = [r.getMessage() for r in caplog.records if r.name == "app.operator.audit"]
    assert lines == [
        f"POST {base} 201 target=application:acme client=10.0.0.5",
        f"POST {base}/acme/credentials 201 target=credential:{credential_id} client=10.0.0.5",
        f"GET {base} 401 target=- client=10.0.0.5",  # refused attempts are recorded too
        f"DELETE {base}/acme 200 target=- client=10.0.0.5",  # the path names the slug
    ], lines
    for line in lines:
        assert TOKEN not in line and secret not in line and "***" not in line, line


def test_a_short_token_turns_the_edge_routes_off_too(monkeypatch):
    from app.edge.settings import EdgeSettings

    env = {
        "ENABLE_LEGACY_API": "false",
        "OPERATOR_ALLOWED_IPS": "93.184.216.34",
        "OPERATOR_API_TOKEN": "too-short",
        "EDGE_ASSERTION_KEYS": "1:test-assertion-key-0123456789abcdef0123",
    }
    short = EdgeSettings.from_env(env)
    assert not short.operator_api_enabled and short.operator_ranges() == []
    full = EdgeSettings.from_env({**env, "OPERATOR_API_TOKEN": TOKEN})
    assert full.operator_api_enabled and full.operator_ranges() == ["93.184.216.34/32"]


def test_a_crashing_operator_call_is_still_audited(session_factory, session, monkeypatch, caplog):
    """An unhandled error inside an endpoint must not leave the call unrecorded."""
    import logging

    from app.services import applications as app_service

    app = make_app(session_factory, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(app_service, "list_applications", boom)
    with (
        caplog.at_level(logging.INFO, logger="app.operator.audit"),
        TestClient(app, client=("10.0.0.5", 1000), raise_server_exceptions=False) as client,
    ):
        assert client.get("/operator/v1/applications", headers=AUTH).status_code == 500
    lines = [r.getMessage() for r in caplog.records if r.name == "app.operator.audit"]
    assert lines == ["GET /operator/v1/applications 500 target=- client=10.0.0.5"], lines


NEW = "rotated-token-abcdefghijklmnopqrstuvwxyz-0123456789"


def test_the_token_can_be_replaced_through_the_api(session_factory, session, monkeypatch, caplog):
    import logging

    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    base = "/operator/v1"
    with (
        TestClient(app, client=("10.0.0.5", 1000)) as client,
        caplog.at_level(logging.INFO, logger="app.operator.audit"),
    ):
        assert client.get(f"{base}/applications", headers=AUTH).status_code == 200
        bad = client.put(f"{base}/token", json={"token": "short"}, headers=AUTH)
        assert bad.status_code == 422
        odd = client.put(f"{base}/token", json={"token": "x" * 31 + " ;rm"}, headers=AUTH)
        assert odd.status_code == 422 and odd.json()["error"]["code"] == "invalid_operator_token"
        assert client.put(f"{base}/token", json={"token": NEW}).status_code == 401

        done = client.put(f"{base}/token", json={"token": NEW}, headers=AUTH)
        assert done.status_code == 204
        # The install-time token is worthless now; the new one works.
        assert client.get(f"{base}/applications", headers=AUTH).status_code == 401
        new_auth = {"Authorization": f"Bearer {NEW}"}
        assert client.get(f"{base}/applications", headers=new_auth).status_code == 200
        # Rotating again needs the current token.
        again = "again-" + NEW
        assert (
            client.put(f"{base}/token", json={"token": again}, headers=new_auth).status_code == 204
        )
        assert client.get(f"{base}/applications", headers=new_auth).status_code == 401
    # Neither token is ever logged, and only a hash is stored.
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert NEW not in logged and TOKEN not in logged and "PUT /operator/v1/token 204" in logged
    from app.models import OperatorToken

    with session_factory() as s:
        [row] = s.query(OperatorToken).all()
        assert row.token_hash != again and len(row.token_hash) == 64


def test_reset_token_brings_back_the_env_token(session_factory, session, monkeypatch, capsys):
    from app import cli
    from app.services import operator_token

    with session_factory() as s:
        operator_token.set_token(s, NEW)
        s.commit()
    monkeypatch.setattr(cli, "get_session_factory", lambda: session_factory)
    assert cli.main(["operator", "token-status"]) == 0
    assert "set through the API" in capsys.readouterr().out
    assert cli.main(["operator", "reset-token"]) == 0
    assert "OPERATOR_API_TOKEN works again" in capsys.readouterr().out
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        assert client.get("/operator/v1/applications", headers=AUTH).status_code == 200
    assert cli.main(["operator", "reset-token"]) == 0
    assert "no token was set" in capsys.readouterr().out
