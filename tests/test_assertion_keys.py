"""Per-application assertion keys: each application's requests are signed with its own key."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.edge.assertion import AssertionInvalid, verify
from app.edge.config import EDGE_TOKEN_HEADER
from app.edge.settings import EdgeConfigurationError
from app.models.types import utcnow
from app.services import assertion_keys
from app.services.errors import AssertionKeyNotFound, InvalidAssertionKey
from tests import test_hardening
from tests.test_hardening import SETTINGS, _ready_domain

client = test_hardening.client  # the API client fixture, with the edge settings

HEADERS = {
    "X-Custom-Domain-Edge-Host": "forms.customer.example",
    EDGE_TOKEN_HEADER: SETTINGS.edge_token,
}


def _assertion(client):
    response = client.get("/internal/edge/assert", headers=HEADERS)
    assert response.status_code == 200
    return response.headers["X-Custom-Domain-Assertion"]


def test_an_application_key_signs_and_the_deployment_key_no_longer_does(
    client, session, make_application
):
    acme = make_application("acme")
    _ready_domain(session, acme)
    deployment = SETTINGS.signing_keys()
    verify(_assertion(client), deployment)  # no key of its own yet

    key, secret = assertion_keys.issue_key(session, acme, activate_in=timedelta(0))
    session.commit()
    token = _assertion(client)
    assert token.split(".")[1] == key.key_id and key.key_id.startswith("app_")
    found = verify(token, {key.key_id: secret.encode()}, expected_application_id=str(acme.id))
    assert found.reference == "ws_1"
    with pytest.raises(AssertionInvalid) as refused:
        verify(token, deployment)
    assert refused.value.code == "unknown_key"


def test_a_new_key_waits_and_issuing_keeps_at_most_two(session, make_application):
    acme = make_application("acme")
    now = utcnow()
    first, _ = assertion_keys.issue_key(session, acme, activate_in=timedelta(0), now=now)
    second, _ = assertion_keys.issue_key(session, acme, now=now)
    assert assertion_keys.signing_key(session, acme.id, now=now).id == first.id
    assert assertion_keys.key_state(second, first, now) == "next"
    # A third replaces the waiting one; the one signing now stays.
    third, _ = assertion_keys.issue_key(session, acme, now=now + timedelta(hours=1))
    assert second.revoked_at is not None and first.revoked_at is None
    later = now + timedelta(hours=26)
    assert assertion_keys.signing_key(session, acme.id, now=later).id == third.id
    assert assertion_keys.key_state(first, third, later) == "previous"
    # Revoking the signing key falls back to the previous one, then to none.
    assertion_keys.revoke_key(session, acme, third.key_id, now=later)
    assert assertion_keys.signing_key(session, acme.id, now=later).id == first.id
    assertion_keys.revoke_key(session, acme, first.key_id, now=later)
    assert assertion_keys.signing_key(session, acme.id, now=later) is None
    with pytest.raises(InvalidAssertionKey):
        assertion_keys.issue_key(session, acme, activate_in=timedelta(days=31))


def test_another_applications_key_is_not_found(session, make_application):
    acme, globex = make_application("acme"), make_application("globex")
    key, _ = assertion_keys.issue_key(session, acme)
    with pytest.raises(AssertionKeyNotFound):
        assertion_keys.revoke_key(session, globex, key.key_id)
    assert key.revoked_at is None


def test_deployment_key_ids_cannot_take_the_application_prefix():
    bad = SETTINGS.__class__(**{**SETTINGS.__dict__, "assertion_keys": (("app_1", "k" * 40),)})
    with pytest.raises(EdgeConfigurationError):
        bad.validate()


def test_the_operator_api(session_factory, session, monkeypatch):
    from tests.test_operator_api import AUTH, make_app

    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    base = "/operator/v1/applications"
    with TestClient(app, client=("10.0.0.5", 1000)) as c:
        created = c.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH).json()
        empty = c.get(f"{base}/acme/assertion-keys", headers=AUTH).json()
        assert empty == {"application_id": created["id"], "signing": None, "keys": []}
        issued = c.post(f"{base}/acme/assertion-keys", json={"activate_in_hours": 0}, headers=AUTH)
        assert issued.status_code == 201
        key = issued.json()
        assert key["state"] == "signing" and key["application_id"] == created["id"]
        assert len(key["secret"]) >= 32
        listed = c.get(f"{base}/acme/assertion-keys", headers=AUTH).json()
        assert listed["signing"] == key["key_id"] and "secret" not in listed["keys"][0]
        nxt = c.post(f"{base}/acme/assertion-keys", headers=AUTH).json()  # default: in 24 hours
        assert nxt["state"] == "next"
        revoked = c.post(f"{base}/acme/assertion-keys/{nxt['key_id']}/revoke", headers=AUTH)
        assert revoked.json()["state"] == "revoked"
        missing = c.post(f"{base}/acme/assertion-keys/app_nope/revoke", headers=AUTH)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "assertion_key_not_found"
        bad = c.post(f"{base}/acme/assertion-keys", json={"activate_in_hours": 9999}, headers=AUTH)
        assert bad.status_code == 422
        assert c.get(f"{base}/acme/assertion-keys").status_code == 401


def test_the_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    create = ["application", "create", "--slug", "acme", "--name", "Acme"]
    assert cli.main([*create, "--cname-target", "edge.example.net"]) == 0
    capsys.readouterr()
    assert cli.main(["assertion-key", "list", "--application", "acme"]) == 0
    assert "deployment key" in capsys.readouterr().out
    issue = ["assertion-key", "issue", "--application", "acme", "--activate-in-hours", "0"]
    assert cli.main(issue) == 0
    out = capsys.readouterr().out
    key_id = next(line.split()[-1] for line in out.splitlines() if line.startswith("key id"))
    assert "shown once" in out
    assert cli.main(["assertion-key", "list", "--application", "acme"]) == 0
    assert f"{key_id}  signing" in capsys.readouterr().out
    revoke = ["assertion-key", "revoke", "--application", "acme", "--key-id", key_id]
    assert cli.main(revoke) == 0
    assert "the deployment key" in capsys.readouterr().out


def test_the_portal(portal):
    from tests.test_portal import csrf_of, sign_in
    from tests.test_portal_manage import _create

    sign_in(portal)
    csrf = csrf_of(portal)
    _create(portal, csrf)
    page = portal.get("/portal/applications/acme/origins").text
    assert "Assertion key" in page and "signed with the deployment" in page
    issued = portal.post(
        "/portal/applications/acme/assertion-keys", data={"csrf": csrf, "activate_in_hours": "0"}
    )
    assert issued.status_code == 200 and "shown only once" in issued.text
    page = portal.get("/portal/applications/acme/origins").text
    assert "signs with its own key" in page and "shown only once" not in page
    bad = portal.post(
        "/portal/applications/acme/assertion-keys", data={"csrf": csrf, "activate_in_hours": "5"}
    )
    assert bad.status_code == 400
