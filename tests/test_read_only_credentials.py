"""Read-only credentials (issue #88): a ``read`` key may only GET and HEAD the v1 API.

The refusal lives in the v1 auth dependency, so these tests enumerate the
application's routes instead of listing them: a POST, PUT, PATCH or DELETE
route added later is covered, or the enumeration test fails.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.models import CredentialScope
from app.services import applications as app_service
from app.services.errors import CredentialReadOnly, InvalidCredential
from app.v1.deps import READ_ONLY_METHODS, current_application
from tests import test_cli, test_v1_api
from tests.test_operator_api import AUTH
from tests.test_portal import csrf_of, session_scope, sign_in
from tests.test_portal_manage import _create

# Fixtures shared with the modules that define them.
cli_env = test_cli.cli_env
make_client = test_v1_api.make_client
_run = test_cli._run
_error = test_v1_api._error

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture
def keys(session, make_application):
    """Full and read-only keys for one application, as Authorization headers."""
    application = make_application("acme")
    _, full = app_service.issue_credential(session, application, label="backend")
    _, read = app_service.issue_credential(session, application, label="dashboard", scope="read")
    session.commit()
    return (
        application,
        {"Authorization": f"Bearer {full}"},
        {"Authorization": f"Bearer {read}"},
    )


def _uses_v1_auth(dependant) -> bool:
    for dependency in dependant.dependencies:
        if dependency.call is current_application or _uses_v1_auth(dependency):
            return True
    return False


def _flatten(routes):
    """Every route the application serves, through included routers too."""
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:  # an included router (FastAPI >= 0.141)
            yield from _flatten(contexts())
        elif getattr(route, "routes", None) is not None and not isinstance(route, APIRoute):
            yield from _flatten(route.routes)  # a mount
        else:
            yield route


def _v1_routes(app) -> list:
    return [
        route
        for route in _flatten(app.routes)
        if getattr(route, "methods", None)
        and (route.path == "/v1" or route.path.startswith("/v1/"))
    ]


def _concrete(path: str) -> str:
    # Every v1 path parameter is an id; a random one names nothing.
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


def _mutating_calls(app) -> list[tuple[str, str]]:
    return sorted(
        (method, route.path)
        for route in _v1_routes(app)
        for method in route.methods
        if method in MUTATING
    )


# --- the service ---------------------------------------------------------------------


def test_scope_defaults_to_full_is_validated_and_survives_rotation(session, make_application):
    application = make_application("acme")
    default, _ = app_service.issue_credential(session, application, label="backend")
    assert default.scope == CredentialScope.FULL
    read, _ = app_service.issue_credential(session, application, label="dash", scope="READ ")
    assert read.scope == CredentialScope.READ
    with pytest.raises(InvalidCredential, match="full or read"):
        app_service.issue_credential(session, application, label="x", scope="write")

    new, _, old = app_service.rotate_credential(session, application, read.id)
    assert old.id == read.id and new.scope == CredentialScope.READ
    new_full, _, _ = app_service.rotate_credential(session, application, default.id)
    assert new_full.scope == CredentialScope.FULL
    session.commit()


def test_the_error_is_a_403_with_a_stable_code():
    from app.v1.errors import _status_for

    assert CredentialReadOnly.code == "credential_read_only"
    assert _status_for(CredentialReadOnly()) == 403
    assert {"GET", "HEAD"} == READ_ONLY_METHODS


# --- v1 enforcement ------------------------------------------------------------------


def test_every_mutating_v1_route_takes_the_auth_dependency(make_client):
    with make_client(legacy=False) as client:
        calls = _mutating_calls(client.app)
        mutating_paths = {path for _, path in calls}
        # The enumeration really sees the routes, the webhook API included.
        assert ("POST", "/v1/domains") in calls
        assert ("DELETE", "/v1/domains/{domain_id}") in calls
        assert ("POST", "/v1/webhooks") in calls
        assert any(p.startswith("/v1/webhooks/") for p in mutating_paths)
        for route in _v1_routes(client.app):
            if set(route.methods) & MUTATING:
                dependant = getattr(route, "dependant", None)
                assert dependant is not None and _uses_v1_auth(dependant), route.path


def test_a_read_key_is_refused_on_every_mutating_route(make_client, keys):
    _, _, read = keys
    with make_client(legacy=False) as client:
        calls = _mutating_calls(client.app)
        assert len(calls) >= 8
        for method, path in calls:
            response = client.request(method, _concrete(path), headers=read, json={})
            assert response.status_code == 403, (method, path, response.text)
            error = _error(response)
            assert error["code"] == "credential_read_only", (method, path)
            assert error["details"] == {"scope": "read", "method": method}


def test_a_full_key_is_never_refused_as_read_only(make_client, keys):
    _, full, _ = keys
    with make_client(legacy=False) as client:
        for method, path in _mutating_calls(client.app):
            response = client.request(method, _concrete(path), headers=full, json={})
            # Unknown ids and empty bodies fail on their own merits, never on scope.
            assert response.status_code != 403, (method, path, response.text)


def test_a_read_key_changes_nothing(make_client, keys, session):
    application, full, read = keys
    body = {"hostname": "forms.customer.example", "reference": "ws_1"}
    with make_client(legacy=False) as client:
        refused = client.post("/v1/domains", headers={**read, "Idempotency-Key": "k1"}, json=body)
        assert refused.status_code == 403
        assert client.get("/v1/domains", headers=read).json()["items"] == []

        created = client.post("/v1/domains", headers=full, json=body)
        assert created.status_code == 201
        domain_id = created.json()["id"]
        assert client.delete(f"/v1/domains/{domain_id}", headers=read).status_code == 403
        assert client.post(f"/v1/domains/{domain_id}/checks", headers=read).status_code == 403
        assert client.get(f"/v1/domains/{domain_id}", headers=read).json()["status"] != "deleting"


def test_a_read_key_can_list_and_get_domains_and_webhooks(make_client, keys, monkeypatch):
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")  # the webhook URL below is local
    _, full, read = keys
    with make_client(legacy=False) as client:
        domain = client.post(
            "/v1/domains",
            headers=full,
            json={"hostname": "forms.customer.example", "reference": "ws_1"},
        ).json()
        webhook = client.post(
            "/v1/webhooks",
            headers=full,
            json={"url": "https://localhost/hooks", "events": ["domain.ready"]},
        )
        assert webhook.status_code == 201, webhook.text
        webhook_id = webhook.json()["id"]

        listing = client.get("/v1/domains", headers=read)
        assert listing.status_code == 200 and listing.json()["items"][0]["id"] == domain["id"]
        assert client.get(f"/v1/domains/{domain['id']}", headers=read).status_code == 200
        webhooks = client.get("/v1/webhooks", headers=read)
        assert webhooks.status_code == 200 and webhooks.json()[0]["id"] == webhook_id
        deliveries = client.get(f"/v1/webhooks/{webhook_id}/deliveries", headers=read)
        assert deliveries.status_code == 200
        # HEAD is allowed by the scope; whatever the route answers, it is not this refusal.
        head = client.head("/v1/domains", headers=read)
        assert head.status_code != 403


def test_cors_preflight_is_unchanged_for_a_read_key(make_client, keys):
    _, _, read = keys
    with make_client(legacy=False) as client:
        preflight = client.options(
            "/v1/domains",
            headers={
                **read,
                "Origin": "https://dashboard.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert "access-control-allow-methods" in preflight.headers


def test_the_request_limit_applies_to_read_keys(make_client, keys, monkeypatch):
    monkeypatch.setenv("V1_REQUESTS_PER_MINUTE", "2")
    _, _, read = keys
    with make_client(legacy=False) as client:
        assert client.get("/v1/domains", headers=read).status_code == 200
        assert client.post("/v1/domains", headers=read, json={}).status_code == 403
        assert client.get("/v1/domains", headers=read).status_code == 429


def test_the_contract_documents_the_refusal(make_client):
    with make_client(legacy=False) as client:
        spec = client.get("/v1/openapi.json").json()
    for path in ("/v1/domains", "/v1/webhooks"):
        response = spec["paths"][path]["post"]["responses"]["403"]
        assert "credential_read_only" in response["description"]


# --- the operator surfaces -----------------------------------------------------------


def test_operator_api_issues_lists_and_rotates_with_scope(session_factory, session, monkeypatch):
    from tests.test_operator_api import make_app

    app = make_app(session_factory, monkeypatch)
    with TestClient(app, client=("10.0.0.5", 1000)) as operator:
        base = "/operator/v1/applications"
        operator.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
        default = operator.post(f"{base}/acme/credentials", json={"label": "b"}, headers=AUTH)
        assert default.status_code == 201 and default.json()["scope"] == "full"
        read = operator.post(
            f"{base}/acme/credentials", json={"label": "dash", "scope": "read"}, headers=AUTH
        )
        assert read.status_code == 201 and read.json()["scope"] == "read"
        bad = operator.post(
            f"{base}/acme/credentials", json={"label": "x", "scope": "admin"}, headers=AUTH
        )
        assert bad.status_code == 422

        listed = operator.get(f"{base}/acme/credentials", headers=AUTH).json()
        assert {c["label"]: c["scope"] for c in listed} == {"b": "full", "dash": "read"}

        rotated = operator.post(
            f"{base}/acme/credentials/{read.json()['id']}/rotate", json={}, headers=AUTH
        )
        assert rotated.status_code == 201 and rotated.json()["scope"] == "read"


def test_cli_issues_lists_and_rotates_with_scope(cli_env, capsys):
    assert (
        _run("application", "create", "--slug", "acme", "--name", "Acme", "--cname-target", "e.x")
        == 0
    )
    capsys.readouterr()
    issue = ("credential", "issue", "--application", "acme")
    assert _run(*issue, "--label", "dash", "--scope", "read") == 0
    out = capsys.readouterr().out
    assert "scope:         read" in out
    credential_id = out.split("credential id:")[1].split()[0]
    assert _run(*issue, "--label", "backend") == 0
    assert "scope:         full" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _run(*issue, "--label", "x", "--scope", "admin")
    capsys.readouterr()

    assert _run("credential", "list", "--application", "acme") == 0
    rows = [line.split("\t") for line in capsys.readouterr().out.strip().splitlines()]
    assert {row[3]: row[2] for row in rows} == {"dash": "read", "backend": "full"}

    assert _run("credential", "rotate", "--application", "acme", "--id", credential_id) == 0
    assert "scope:             read" in capsys.readouterr().out


def test_portal_issues_a_read_only_key_and_shows_access(portal, session):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    base = "/portal/applications/acme/credentials"

    issued = client.post(base, data={"csrf": csrf, "label": "dash", "scope": "read"})
    assert issued.status_code == 200
    assert 'Copy the new read-only API key "dash"' in issued.text
    client.post(base, data={"csrf": csrf, "label": "backend"})
    bad = client.post(base, data={"csrf": csrf, "label": "x", "scope": "admin"})
    assert bad.status_code == 400 and "full or read" in bad.text
    # Without the session's CSRF token nothing is issued.
    client.post(base, data={"csrf": "nope", "label": "forged", "scope": "read"})

    page = client.get(base).text
    assert '<th scope="col">Access</th>' in page
    assert ">Read only<" in page and ">Full access<" in page
    assert '<select id="scope" name="scope"' in page

    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        scopes = {c.label: c.scope for c in app_service.list_credentials(s, acme)}
        assert scopes == {"dash": CredentialScope.READ, "backend": CredentialScope.FULL}
        dash_id = next(c.id for c in app_service.list_credentials(s, acme) if c.label == "dash")

    rotated = client.post(f"{base}/{dash_id}/rotate", data={"csrf": csrf, "grace_hours": "1"})
    assert 'Copy the new read-only API key "dash"' in rotated.text
    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        dashes = [c for c in app_service.list_credentials(s, acme) if c.label == "dash"]
        assert len(dashes) == 2 and all(c.scope == CredentialScope.READ for c in dashes)


# --- the SDK -------------------------------------------------------------------------


def test_sdk_raises_a_typed_error_for_a_read_only_key():
    from custom_domain import (
        ApiError,
        Client,
        CredentialReadOnlyError,
        PermissionDeniedError,
    )

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})
        body = {
            "error": {
                "code": "credential_read_only",
                "message": "read-only",
                "details": {"scope": "read", "method": request.method},
            }
        }
        return httpx.Response(403, json=body)

    client = Client(
        "http://api.example",
        credential="cd_" + "x" * 43,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    client.list_domains()
    with pytest.raises(CredentialReadOnlyError) as info:
        client.delete_domain(uuid.uuid4())
    assert isinstance(info.value, PermissionDeniedError) and isinstance(info.value, ApiError)
    assert info.value.status == 403 and info.value.details["method"] == "DELETE"
    assert calls == ["GET", "DELETE"]  # a refusal is not retried


def test_sdk_maps_other_403s_to_permission_denied():
    from custom_domain.errors import CredentialReadOnlyError, PermissionDeniedError, error_for

    other = error_for(403, "application_suspended", "suspended")
    assert type(other) is PermissionDeniedError
    assert type(error_for(403, "credential_read_only", "x")) is CredentialReadOnlyError


def test_expiry_and_scope_are_independent(session, make_application):
    from app.models.types import utcnow

    application = make_application("acme")
    credential, _ = app_service.issue_credential(
        session, application, label="d", scope="read", expires_at=utcnow() + timedelta(days=1)
    )
    session.commit()
    assert credential.scope == CredentialScope.READ and credential.expires_at is not None
