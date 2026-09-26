import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_session
from app.main import create_app
from app.models import ApplicationStatus, CheckStatus, CheckType, DomainStatus, EventType
from app.services.applications import issue_credential, set_application_status
from app.services.domains import (
    get_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)


@pytest.fixture
def make_client(session_factory, monkeypatch):
    def _make(legacy: bool = True) -> TestClient:
        monkeypatch.setenv("ENABLE_LEGACY_API", "true" if legacy else "false")
        # Tests never talk to a real Caddy: keep the reconciler loop off unless
        # a test sets EDGE_RECONCILE_ENABLED itself.
        monkeypatch.setenv(
            "EDGE_RECONCILE_ENABLED", os.environ.get("EDGE_RECONCILE_ENABLED", "false")
        )
        monkeypatch.setenv("DNS_WORKER_ENABLED", "false")
        monkeypatch.setenv("WEBHOOK_WORKER_ENABLED", "false")
        app = create_app()

        def override():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_session] = override
        return TestClient(app)

    return _make


@pytest.fixture
def client(make_client):
    with make_client() as client:
        yield client


@pytest.fixture
def tenant(session, make_application):
    def _make(slug: str = "acme"):
        application = make_application(slug)
        _, secret = issue_credential(session, application, label="test")
        session.commit()
        return application, {"Authorization": f"Bearer {secret}"}

    return _make


BODY = {"hostname": "Forms.Customer.Example.", "reference": "ws_1", "metadata": {"plan": "pro"}}


def _error(response):
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    return body["error"]


def test_requests_without_valid_credential_are_unauthorized(client, tenant):
    _, headers = tenant()
    for bad in ({}, {"Authorization": "Bearer cd_nope"}, {"Authorization": "Basic abc"}):
        response = client.get("/v1/domains", headers=bad)
        assert response.status_code == 401
        assert _error(response)["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"] == "Bearer"
    # The legacy query-string key is not accepted by v1.
    secret = headers["Authorization"].split()[1]
    assert client.get(f"/v1/domains?api_key={secret}").status_code == 401
    assert client.get("/v1/domains", headers=headers).status_code == 200


def test_suspended_application_is_rejected(client, session, tenant):
    application, headers = tenant()
    set_application_status(session, application, ApplicationStatus.SUSPENDED)
    session.commit()
    response = client.post("/v1/domains", json=BODY, headers=headers)
    assert response.status_code == 401


def test_create_normalizes_and_returns_dns_records(client, tenant):
    _, headers = tenant()
    response = client.post("/v1/domains", json=BODY, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    uuid.UUID(body["id"])
    assert body["hostname"] == "forms.customer.example"
    assert body["reference"] == "ws_1"
    assert body["status"] == "pending_dns"
    assert body["metadata"] == {"plan": "pro"}
    assert body["deleted_at"] is None
    txt, cname = body["dns_records"]
    assert txt["type"] == "TXT" and txt["purpose"] == "ownership"
    assert txt["name"] == "_custom-domain-challenge.forms.customer.example"
    assert txt["value"].startswith("custom-domain-verify=")
    assert txt["help"]
    assert cname == {
        "name": "forms.customer.example",
        "type": "CNAME",
        "value": "acme.edge.example.net",
        "purpose": "routing",
        "help": cname["help"],
    }
    assert [c["type"] for c in body["checks"]] == ["ownership", "routing", "certificate", "origin"]
    assert all(c["status"] == "pending" and c["error_code"] is None for c in body["checks"])


@pytest.mark.parametrize(
    ("payload", "code", "fragment"),
    [
        ({**BODY, "upstream": "evil.example:443"}, "validation_error", "upstream"),
        ({"hostname": "customer.example", "reference": "ws_1"}, "apex_not_supported", "hostname"),
        (
            {"hostname": "*.customer.example", "reference": "ws_1"},
            "wildcard_not_supported",
            "hostname",
        ),
        ({"hostname": "forms.customer.example"}, "validation_error", "reference"),
        ({"hostname": "forms.customer.example", "reference": ""}, "validation_error", "reference"),
        (
            {**BODY, "metadata": {f"k{i}": i for i in range(33)}},
            "validation_error",
            "metadata",
        ),
        ({**BODY, "metadata": {"nested": {"a": 1}}}, "validation_error", "metadata"),
    ],
)
def test_create_rejects_invalid_requests(client, tenant, payload, code, fragment):
    _, headers = tenant()
    response = client.post("/v1/domains", json=payload, headers=headers)
    assert response.status_code == 422, response.text
    error = _error(response)
    assert error["code"] == code
    assert fragment in str(error["details"])


def test_hostname_cannot_be_live_in_two_applications(client, tenant):
    _, acme = tenant("acme")
    _, globex = tenant("globex")
    assert client.post("/v1/domains", json=BODY, headers=acme).status_code == 201
    for headers in (acme, globex):
        response = client.post("/v1/domains", json={**BODY, "reference": "other"}, headers=headers)
        assert response.status_code == 409
        assert _error(response)["code"] == "hostname_already_claimed"


def test_reads_and_deletes_are_application_scoped(client, tenant):
    _, acme = tenant("acme")
    _, globex = tenant("globex")
    domain_id = client.post("/v1/domains", json=BODY, headers=acme).json()["id"]

    for method, path in (
        ("GET", f"/v1/domains/{domain_id}"),
        ("DELETE", f"/v1/domains/{domain_id}"),
        ("POST", f"/v1/domains/{domain_id}/checks"),
    ):
        response = client.request(method, path, headers=globex)
        assert response.status_code == 404, (method, response.text)
        assert _error(response)["code"] == "domain_not_found"
    assert client.get(f"/v1/domains/{domain_id}", headers=acme).status_code == 200
    assert client.get("/v1/domains", headers=globex).json()["items"] == []

    response = client.get("/v1/domains/not-a-uuid", headers=acme)
    assert response.status_code == 422 and _error(response)["code"] == "validation_error"


def test_list_filters_and_paginates(client, session, tenant):
    application, headers = tenant()
    ids = []
    for index in range(3):
        body = {"hostname": f"site{index}.customer.example", "reference": f"ws_{index}"}
        ids.append(client.post("/v1/domains", json=body, headers=headers).json()["id"])

    page = client.get("/v1/domains", headers=headers, params={"limit": 2}).json()
    assert [d["id"] for d in page["items"]] == ids[:2]
    assert page["next_offset"] == 2
    page = client.get("/v1/domains", headers=headers, params={"limit": 2, "offset": 2}).json()
    assert [d["id"] for d in page["items"]] == ids[2:]
    assert page["next_offset"] is None

    page = client.get("/v1/domains", headers=headers, params={"reference": "ws_1"}).json()
    assert [d["reference"] for d in page["items"]] == ["ws_1"]

    domain = get_domain(session, application, uuid.UUID(ids[0]))
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    page = client.get("/v1/domains", headers=headers, params={"status": "ready"}).json()
    assert [d["id"] for d in page["items"]] == [ids[0]]
    assert all(c["status"] == "passing" for c in page["items"][0]["checks"])
    assert client.get("/v1/domains", headers=headers, params={"status": "bogus"}).status_code == 422
    assert client.get("/v1/domains", headers=headers, params={"limit": 0}).status_code == 422


def test_idempotency_key_makes_create_safe_to_retry(client, tenant):
    _, headers = tenant()
    keyed = {**headers, "Idempotency-Key": "req-123"}
    first = client.post("/v1/domains", json=BODY, headers=keyed)
    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first.headers

    replay = client.post("/v1/domains", json=BODY, headers=keyed)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]

    reused = client.post("/v1/domains", json={**BODY, "reference": "ws_9"}, headers=keyed)
    assert reused.status_code == 422
    assert _error(reused)["code"] == "idempotency_key_reused"

    other_key = client.post(
        "/v1/domains", json=BODY, headers={**headers, "Idempotency-Key": "req-456"}
    )
    assert other_key.status_code == 409

    # Keys are scoped per application: another tenant may reuse the string.
    _, globex = tenant("globex")
    response = client.post(
        "/v1/domains",
        json={"hostname": "other.customer.example", "reference": "g_1"},
        headers={**globex, "Idempotency-Key": "req-123"},
    )
    assert response.status_code == 201


def test_recheck_marks_checks_due_and_records_event(client, session, tenant):
    application, headers = tenant()
    domain_id = client.post("/v1/domains", json=BODY, headers=headers).json()["id"]
    response = client.post(f"/v1/domains/{domain_id}/checks", headers=headers)
    assert response.status_code == 202
    assert response.json()["id"] == domain_id

    domain = get_domain(session, application, uuid.UUID(domain_id))
    assert all(check.next_check_at is not None for check in domain.checks)
    assert EventType.RECHECK_REQUESTED in [e.event_type for e in domain.events]

    client.delete(f"/v1/domains/{domain_id}", headers=headers)
    response = client.post(f"/v1/domains/{domain_id}/checks", headers=headers)
    assert response.status_code == 409
    assert _error(response)["code"] == "invalid_status_transition"


def test_recheck_is_rate_limited_per_domain(client, tenant):
    _, headers = tenant()
    domain_id = client.post("/v1/domains", json=BODY, headers=headers).json()["id"]
    assert client.post(f"/v1/domains/{domain_id}/checks", headers=headers).status_code == 202

    limited = client.post(f"/v1/domains/{domain_id}/checks", headers=headers)
    assert limited.status_code == 429
    error = _error(limited)
    assert error["code"] == "rate_limited"
    retry_after = int(limited.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert error["details"]["retry_after_seconds"] == retry_after


def test_recheck_budget_is_application_scoped(client, monkeypatch, tenant):
    from app.services import domains as domain_service

    monkeypatch.setattr(domain_service, "RECHECK_MAX_PER_WINDOW", 2)
    _, acme = tenant("acme")
    _, globex = tenant("globex")
    ids = [
        client.post(
            "/v1/domains",
            json={"hostname": f"s{i}.customer.example", "reference": "w"},
            headers=acme,
        ).json()["id"]
        for i in range(3)
    ]
    assert client.post(f"/v1/domains/{ids[0]}/checks", headers=acme).status_code == 202
    assert client.post(f"/v1/domains/{ids[1]}/checks", headers=acme).status_code == 202
    over = client.post(f"/v1/domains/{ids[2]}/checks", headers=acme)
    assert over.status_code == 429 and "Retry-After" in over.headers

    # Another application has its own budget.
    other = client.post(
        "/v1/domains", json={"hostname": "g.customer.example", "reference": "w"}, headers=globex
    ).json()["id"]
    assert client.post(f"/v1/domains/{other}/checks", headers=globex).status_code == 202


def test_idempotency_key_expires_without_the_purge_job(client, session, tenant):
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import IdempotencyKey
    from app.models.types import utcnow

    _, headers = tenant()
    keyed = {**headers, "Idempotency-Key": "expiring"}
    first = client.post("/v1/domains", json=BODY, headers=keyed)
    assert first.status_code == 201

    row = session.scalar(select(IdempotencyKey).where(IdempotencyKey.key == "expiring"))
    row.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    new_body = {"hostname": "second.customer.example", "reference": "ws_2"}
    reused = client.post("/v1/domains", json=new_body, headers=keyed)
    assert reused.status_code == 201, reused.text
    assert reused.json()["id"] != first.json()["id"]

    replay = client.post("/v1/domains", json=new_body, headers=keyed)
    assert replay.status_code == 200
    assert replay.json()["id"] == reused.json()["id"]
    stale = client.post("/v1/domains", json=BODY, headers=keyed)
    assert stale.status_code == 422 and _error(stale)["code"] == "idempotency_key_reused"


def test_pagination_lookahead_at_maximum_page_size(client, session, tenant, monkeypatch):
    from app.services import domains as domain_service
    from app.services.domains import MAX_PAGE_SIZE, claim_domain

    monkeypatch.setattr(domain_service, "REGISTRATION_MAX_PER_WINDOW", 10_000)

    application, headers = tenant()
    for index in range(MAX_PAGE_SIZE + 1):
        claim_domain(session, application, f"n{index}.customer.example", "w")
    session.commit()

    page = client.get("/v1/domains", headers=headers, params={"limit": MAX_PAGE_SIZE}).json()
    assert len(page["items"]) == MAX_PAGE_SIZE
    assert page["next_offset"] == MAX_PAGE_SIZE
    last = client.get(
        "/v1/domains", headers=headers, params={"limit": MAX_PAGE_SIZE, "offset": MAX_PAGE_SIZE}
    ).json()
    assert len(last["items"]) == 1 and last["next_offset"] is None
    assert (
        client.get("/v1/domains", headers=headers, params={"limit": MAX_PAGE_SIZE + 1}).status_code
        == 422
    )


def test_delete_tombstones_and_frees_the_hostname(client, tenant):
    _, acme = tenant("acme")
    _, globex = tenant("globex")
    created = client.post("/v1/domains", json=BODY, headers=acme).json()
    domain_id = created["id"]

    response = client.delete(f"/v1/domains/{domain_id}", headers=acme)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "deleting"
    assert body["deleted_at"] is not None
    assert body["dns_records"] == []

    assert client.get(f"/v1/domains/{domain_id}", headers=acme).status_code == 404
    shown = client.get(f"/v1/domains/{domain_id}", headers=acme, params={"include_deleted": "true"})
    assert shown.status_code == 200 and shown.json()["status"] == "deleting"
    assert client.get("/v1/domains", headers=acme).json()["items"] == []
    listed = client.get("/v1/domains", headers=acme, params={"include_deleted": "true"}).json()
    assert [d["id"] for d in listed["items"]] == [domain_id]

    again = client.delete(f"/v1/domains/{domain_id}", headers=acme)
    assert again.status_code == 202 and again.json()["deleted_at"] == body["deleted_at"]

    reclaimed = client.post("/v1/domains", json={**BODY, "reference": "g_1"}, headers=globex)
    assert reclaimed.status_code == 201
    assert reclaimed.json()["id"] != domain_id
    assert reclaimed.json()["dns_records"][0]["value"] != created["dns_records"][0]["value"]


def test_delete_triggers_edge_reconcile_when_enabled(make_client, tenant):
    from app.edge.config import hostnames_in
    from app.edge.reconcile import Reconciler
    from app.edge.settings import EdgeSettings

    class Recorder:
        def __init__(self):
            self.runs = 0

        def run_once(self):
            self.runs += 1

    with make_client(legacy=False) as client:
        _, headers = tenant()
        recorder = Recorder()
        client.app.state.reconciler = recorder
        domain_id = client.post("/v1/domains", json=BODY, headers=headers).json()["id"]
        assert client.delete(f"/v1/domains/{domain_id}", headers=headers).status_code == 202
        assert recorder.runs == 1
    assert isinstance(Reconciler, type) and callable(hostnames_in) and EdgeSettings


def test_app_refuses_to_start_with_legacy_api_and_reconciler(make_client, monkeypatch):
    monkeypatch.setenv("EDGE_RECONCILE_ENABLED", "true")
    with pytest.raises(Exception, match="cannot both be true"), make_client(legacy=True):
        pass


def test_legacy_api_is_deprecated_and_can_be_disabled(make_client):
    with make_client(legacy=True) as client:
        response = client.get("/domains")
        assert response.status_code == 403  # legacy key required; the route exists
        spec = client.get("/v1/openapi.json").json()
        assert spec["paths"]["/domains"]["post"]["deprecated"] is True
        assert "upstream" in str(spec["paths"]["/domains"]["post"]["parameters"])
    with make_client(legacy=False) as client:
        assert client.get("/domains").status_code == 404
        assert client.get("/docs").status_code == 404
        assert "/domains" not in client.get("/v1/openapi.json").json()["paths"]
        assert client.get("/v1/docs").status_code == 200


def test_internal_tls_ask_follows_certificate_authorization(client, session, tenant, monkeypatch):
    from app.services.domains import get_domain, mark_claim_verified

    application, headers = tenant()
    domain_id = client.post("/v1/domains", json=BODY, headers=headers).json()["id"]
    ask = "/internal/tls/ask"
    # TestClient's address is "testclient"; trust it for this test.
    client.app.state.edge_settings = client.app.state.edge_settings.__class__(
        **{**client.app.state.edge_settings.__dict__, "ask_trusted_hosts": ("testclient",)}
    )

    assert client.get(ask, params={"domain": "forms.customer.example"}).status_code == 403
    domain = get_domain(session, application, uuid.UUID(domain_id))
    mark_claim_verified(session, domain)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    session.commit()
    assert client.get(ask, params={"domain": "Forms.Customer.Example."}).status_code == 200
    assert client.get(ask, params={"domain": "unknown.customer.example"}).status_code == 403
    assert client.get(ask, params={"domain": "*.customer.example"}).status_code == 403
    assert client.get(ask).status_code == 422

    client.delete(f"/v1/domains/{domain_id}", headers=headers)
    assert client.get(ask, params={"domain": "forms.customer.example"}).status_code == 403

    # The edge holds a certificate for its own names: the CNAME target of an
    # active application is allowed (canonicalized), other names are not.
    target = application.cname_target
    assert client.get(ask, params={"domain": target}).status_code == 200
    assert client.get(ask, params={"domain": target.upper() + "."}).status_code == 200
    assert client.get(ask, params={"domain": "other." + target}).status_code == 403
    from app.models import ApplicationStatus

    application.status = ApplicationStatus.SUSPENDED
    session.commit()
    assert client.get(ask, params={"domain": target}).status_code == 403
    application.status = ApplicationStatus.ACTIVE
    session.commit()

    # After the target changes, the former name stays authorized while a live
    # claim still names it, and stops once that claim is re-issued.
    from app.services.applications import set_cname_target
    from app.services.domains import reissue_claim

    kept_id = client.post(
        "/v1/domains", json={**BODY, "hostname": "kept.customer.example"}, headers=headers
    ).json()["id"]
    set_cname_target(session, application, "moved.edge.example")
    session.commit()
    assert client.get(ask, params={"domain": "moved.edge.example"}).status_code == 200
    assert client.get(ask, params={"domain": target}).status_code == 200  # former, still named
    reissue_claim(session, application, uuid.UUID(kept_id))
    session.commit()
    assert client.get(ask, params={"domain": target}).status_code == 403

    # Untrusted client addresses are refused regardless of the domain.
    client.app.state.edge_settings = client.app.state.edge_settings.__class__(
        **{**client.app.state.edge_settings.__dict__, "ask_trusted_hosts": ("127.0.0.1",)}
    )
    assert client.get(ask, params={"domain": "forms.customer.example"}).status_code == 403
    assert "/internal/tls/ask" not in client.get("/v1/openapi.json").json()["paths"]
