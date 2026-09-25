"""SDK against the real API app, plus cross-checks with the service's own algorithms."""

import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from custom_domain import (
    AssertionInvalid,
    AuthenticationError,
    Client,
    ConflictError,
    CustomDomainMiddleware,
    NotFoundError,
    RateLimitedError,
    ServerError,
    SignatureInvalid,
    TransportError,
    ValidationError,
    WorkspaceResolver,
    parse_event,
    verify_assertion,
    verify_webhook,
)
from custom_domain.assertion import HEADER as ASSERTION_HEADER
from fastapi.testclient import TestClient

from app.db.session import get_session
from app.edge.assertion import sign as service_sign
from app.main import create_app
from app.models import CheckStatus, CheckType, DomainStatus
from app.services.applications import issue_credential
from app.services.domains import (
    claim_domain,
    get_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)
from app.webhooks.signature import sign as service_sign_webhook

KEYS = {"1": "k" * 40}


@pytest.fixture
def api(session_factory, monkeypatch):
    """The API served over a real socket, since the sync SDK client needs one."""
    import socket
    import threading
    import time as _time

    import uvicorn

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")
    app = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.time() + 15
    while _time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            _time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def sdk(api, session, make_application):
    acme = make_application("acme")
    _, secret = issue_credential(session, acme, label="sdk")
    session.commit()
    client = Client(api, credential=secret, sleep=lambda s: None)
    yield client, acme
    client.close()


def test_client_rejects_non_credentials():
    with pytest.raises(ValueError):
        Client("http://x", credential="not-a-credential")


def test_domain_lifecycle_through_sdk(sdk, session):
    client, acme = sdk
    domain = client.create_domain(
        "Forms.Customer.Example", "ws_1", metadata={"plan": "pro"}, idempotency_key="k1"
    )
    assert domain.hostname == "forms.customer.example" and domain.status == "pending_dns"
    assert not domain.is_ready and domain.metadata == {"plan": "pro"}
    txt, cname = domain.dns_records
    assert (
        txt.type == "TXT"
        and txt.purpose == "ownership"
        and txt.value.startswith("custom-domain-verify=")
    )
    assert cname.type == "CNAME" and cname.value == "acme.edge.example.net"
    text = domain.render_dns_instructions()
    assert "_custom-domain-challenge.forms.customer.example" in text and "purpose: routing" in text
    assert [c.type for c in domain.checks] == ["ownership", "routing", "certificate", "origin"]
    assert domain.check("ownership").status == "pending"

    replay = client.create_domain(
        "Forms.Customer.Example", "ws_1", metadata={"plan": "pro"}, idempotency_key="k1"
    )
    assert replay.id == domain.id

    with pytest.raises(ConflictError) as info:
        client.create_domain("forms.customer.example", "ws_2")
    assert info.value.code == "hostname_already_claimed" and info.value.status == 409
    with pytest.raises(ValidationError) as info:
        client.create_domain("customer.example", "ws_2")
    assert info.value.code == "apex_not_supported"
    with pytest.raises(ValidationError) as info:
        client.create_domain("forms.customer.example", "ws_other", idempotency_key="k1")
    assert info.value.code == "idempotency_key_reused"

    fetched = client.get_domain(domain.id)
    assert fetched.id == domain.id and fetched.created_at.tzinfo is not None
    with pytest.raises(NotFoundError):
        client.get_domain(uuid.uuid4())

    for index in range(3):
        client.create_domain(f"s{index}.customer.example", "ws_page", idempotency_key=f"p{index}")
    page = client.list_domains(reference="ws_page", limit=2)
    assert len(page.items) == 2 and page.next_offset == 2
    assert [d.reference for d in client.iter_domains(reference="ws_page", page_size=2)] == [
        "ws_page"
    ] * 3
    assert client.list_domains(status="ready").items == []

    rechecked = client.request_recheck(domain.id)
    assert rechecked.id == domain.id
    with pytest.raises(RateLimitedError) as info:
        client.request_recheck(domain.id)
    assert info.value.retry_after >= 1

    deleted = client.delete_domain(domain.id)
    assert (
        deleted.status == "deleting"
        and deleted.deleted_at is not None
        and deleted.dns_records == []
    )
    with pytest.raises(NotFoundError):
        client.get_domain(domain.id)
    assert client.get_domain(domain.id, include_deleted=True).status == "deleting"


def test_authentication_errors(api, session, make_application):
    make_application("acme")
    client = Client(api, credential="cd_" + "x" * 43)
    with pytest.raises(AuthenticationError) as info:
        client.list_domains()
    assert info.value.code == "unauthorized"


def test_retry_policy_only_for_safe_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        n = len([c for c in calls if c[1] == request.url.path])
        if request.url.path == "/v1/domains/flaky":
            if n < 3:
                return httpx.Response(
                    503, json={"error": {"code": "unavailable", "message": "x", "details": {}}}
                )
            return httpx.Response(200, json=_domain_json())
        if request.url.path == "/v1/domains" and request.method == "POST":
            if request.headers.get("Idempotency-Key") and n < 2:
                return httpx.Response(
                    503, json={"error": {"code": "unavailable", "message": "x", "details": {}}}
                )
            if not request.headers.get("Idempotency-Key"):
                return httpx.Response(
                    503, json={"error": {"code": "unavailable", "message": "x", "details": {}}}
                )
            return httpx.Response(201, json=_domain_json())
        if request.url.path == "/v1/domains/limited":
            if n < 2:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "3"},
                    json={"error": {"code": "rate_limited", "message": "slow", "details": {}}},
                )
            return httpx.Response(200, json=_domain_json())
        if request.url.path == "/v1/domains/broken":
            return httpx.Response(500, text="boom")
        if request.url.path == "/v1/domains/gone":
            raise httpx.ConnectError("refused")
        return httpx.Response(
            404, json={"error": {"code": "domain_not_found", "message": "no", "details": {}}}
        )

    sleeps = []
    client = Client(
        "http://api",
        credential="cd_x",
        transport=httpx.MockTransport(handler),
        max_retries=2,
        sleep=sleeps.append,
    )

    assert client.get_domain("flaky").id  # two 503s then success
    assert len([c for c in calls if c[1] == "/v1/domains/flaky"]) == 3 and sleeps == [0.5, 1.0]

    sleeps.clear()
    client.create_domain("a.b.example", "w", idempotency_key="key")  # retried once with the key
    assert len([c for c in calls if c[1] == "/v1/domains" and c[2] == "key"]) == 2

    with pytest.raises(ServerError):
        client.create_domain("a.b.example", "w")  # never retried without a key
    assert len([c for c in calls if c[1] == "/v1/domains" and c[2] is None]) == 1

    sleeps.clear()
    assert client.get_domain("limited").id
    assert sleeps == [3]  # Retry-After honoured

    with pytest.raises(ServerError) as info:
        client.get_domain("broken")
    assert (
        info.value.code == "http_error"
        and len([c for c in calls if c[1] == "/v1/domains/broken"]) == 3
    )

    with pytest.raises(TransportError):
        client.get_domain("gone")


def _domain_json(**overrides):
    base = {
        "id": str(uuid.uuid4()),
        "hostname": "a.b.example",
        "reference": "w",
        "status": "pending_dns",
        "dns_records": [],
        "checks": [],
        "metadata": None,
        "created_at": "2026-09-25T10:00:00+00:00",
        "updated_at": "2026-09-25T10:00:00+00:00",
        "deleted_at": None,
    }
    base.update(overrides)
    return base


def test_assertion_cross_verification():
    token = service_sign(
        key_id="1",
        key=KEYS["1"].encode(),
        application_id="app-1",
        domain_id="dom-1",
        reference="ws_42",
        hostname="forms.customer.example",
        request_id="r1",
        now=1_000_000,
    )
    assertion = verify_assertion(
        token,
        KEYS,
        expected_application_id="app-1",
        expected_hostname="Forms.Customer.Example",
        now=1_000_010,
    )
    assert assertion.reference == "ws_42" and assertion.request_id == "r1"
    for kwargs, code in (
        ({"expected_application_id": "app-2"}, "wrong_application"),
        ({"expected_application_id": "app-1", "now": 1_000_000 + 100}, "expired"),
        (
            {"expected_application_id": "app-1", "expected_hostname": "other.example"},
            "wrong_hostname",
        ),
    ):
        with pytest.raises(AssertionInvalid) as info:
            verify_assertion(token, KEYS, **{"now": 1_000_010, **kwargs})
        assert info.value.code == code
    with pytest.raises(AssertionInvalid) as info:
        verify_assertion(token, {"2": "other"}, expected_application_id="app-1", now=1_000_010)
    assert info.value.code == "unknown_key"

    resolver = WorkspaceResolver(KEYS, "app-1")
    fresh = service_sign(
        key_id="1",
        key=KEYS["1"].encode(),
        application_id="app-1",
        domain_id="dom-1",
        reference="ws_42",
        hostname="forms.customer.example",
        request_id="r1",
    )
    assert (
        resolver.resolve({ASSERTION_HEADER: fresh}, "forms.customer.example:443").reference
        == "ws_42"
    )
    with pytest.raises(AssertionInvalid) as info:
        resolver.resolve({}, "forms.customer.example")
    assert info.value.code == "missing"


def test_middleware_resolves_workspace_and_serves_probe():
    from examples.sample_saas.app import create_app as sample_app

    app = sample_app(keys=KEYS, application_id="app-1")
    client = TestClient(app)
    token = service_sign(
        key_id="1",
        key=KEYS["1"].encode(),
        application_id="app-1",
        domain_id="dom-1",
        reference="ws_alpha",
        hostname="alpha.sample.localtest.me",
        request_id="r1",
    )
    headers = {ASSERTION_HEADER: token, "Host": "alpha.sample.localtest.me"}
    page = client.get("/", headers=headers)
    assert page.status_code == 200 and "Alpha Forms" in page.text and "ws_alpha" in page.text

    probe = client.get("/.well-known/custom-domain-workspace", headers=headers)
    assert probe.status_code == 200
    assert probe.json() == {"reference": "ws_alpha", "application": "app-1"}

    assert client.get("/", headers={"Host": "alpha.sample.localtest.me"}).status_code == 403
    assert (
        client.get("/", headers={"Host": "alpha.sample.localtest.me"}).json()["error"]
        == "assertion_missing"
    )
    wrong_host = client.get(
        "/", headers={ASSERTION_HEADER: token, "Host": "beta.sample.localtest.me"}
    )
    assert (
        wrong_host.status_code == 403 and wrong_host.json()["error"] == "assertion_wrong_hostname"
    )
    assert client.get("/.well-known/custom-domain-workspace").status_code == 403

    other = service_sign(
        key_id="1",
        key=KEYS["1"].encode(),
        application_id="app-1",
        domain_id="dom-2",
        reference="ws_nobody",
        hostname="x.sample.localtest.me",
        request_id="r2",
    )
    assert (
        client.get(
            "/", headers={ASSERTION_HEADER: other, "Host": "x.sample.localtest.me"}
        ).status_code
        == 404
    )

    passthrough = CustomDomainMiddleware(
        lambda scope, receive, send: None,
        keys=KEYS,
        application_id="app-1",
        on_missing="passthrough",
    )
    assert passthrough.on_missing == "passthrough"
    with pytest.raises(ValueError):
        CustomDomainMiddleware(
            lambda *a: None, keys=KEYS, application_id="app-1", on_missing="ignore"
        )


def test_webhook_verification_and_parsing_cross_check():
    body = (
        b'{"id": "e1", "type": "domain.ready", "created_at": "2026-09-25T15:00:00+00:00", "data": {"domain": '
        + __import__("json").dumps(_domain_json(status="ready")).encode()
        + b"}}"
    )
    header = service_sign_webhook(body, ["whsec_old", "whsec_new"], timestamp=1_000_000)
    assert verify_webhook(header, body, ["whsec_new"], now=1_000_100) == 1_000_000
    with pytest.raises(SignatureInvalid) as info:
        verify_webhook(header, body, ["whsec_other"], now=1_000_100)
    assert info.value.code == "bad_signature"
    with pytest.raises(SignatureInvalid) as info:
        verify_webhook(header, body, ["whsec_new"], now=1_000_000 + 1000)
    assert info.value.code == "stale"
    event = parse_event(body)
    assert event.type == "domain.ready" and event.domain.status == "ready" and event.domain.is_ready
    assert event.created_at == datetime(2026, 9, 25, 15, 0, tzinfo=UTC)


def test_webhook_methods_through_sdk(sdk, session):
    client, acme = sdk
    hook = client.create_webhook("https://localhost/hooks", ["domain.ready"])
    assert hook.secret.startswith("whsec_") and hook.active
    assert [h.id for h in client.list_webhooks()] == [hook.id] and client.list_webhooks()[
        0
    ].secret is None

    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()

    deliveries = client.list_deliveries(hook.id)
    assert (
        len(deliveries) == 1
        and deliveries[0].state == "pending"
        and deliveries[0].event_type == "domain.ready"
    )
    assert client.replay_delivery(hook.id, deliveries[0].id).state == "pending"
    assert client.replay_since(hook.id, datetime(2020, 1, 1, tzinfo=UTC)) == 1
    rotated = client.rotate_webhook_secret(hook.id)
    assert rotated.secret and rotated.secret != hook.secret
    assert client.revoke_webhook(hook.id).active is False
    assert get_domain(session, acme, domain.id).status == DomainStatus.READY
    assert time.time() > 0
