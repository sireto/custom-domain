import json
import threading
import time
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.models import CheckStatus, CheckType, DomainStatus, WebhookDelivery
from app.models.types import utcnow
from app.services.domains import (
    claim_domain,
    delete_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)
from app.services.webhooks import (
    InvalidWebhook,
    WebhookNotFound,
    create_subscription,
    list_deliveries,
    replay_delivery,
    replay_since,
    revoke_subscription,
    rotate_secret,
    signing_secrets,
)
from app.webhooks.signature import HEADER, SignatureInvalid, sign, verify
from app.webhooks.worker import BACKOFF, MAX_ATTEMPTS, WebhookWorker, attempt_delivery, deliver_due

URL = "https://localhost/custom-domain"  # resolves; allowed in tests via allow_private
ALL_EVENTS = ["domain.ready", "domain.attention_required", "domain.recovered", "domain.deleted"]


def _subscribe(session, application, events=ALL_EVENTS, url=URL):
    subscription, secret = create_subscription(
        session, application, url=url, events=events, allow_private=True
    )
    session.commit()
    return subscription, secret


def _make_ready(session, domain):
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY, reason="readiness_probe_passed")
    session.commit()


# --- signatures ----------------------------------------------------------------


def test_signature_round_trip_rotation_and_rejections():
    body = b'{"id": "e1"}'
    header = sign(body, ["old-secret", "new-secret"], timestamp=1_000_000)
    assert header.startswith("t=1000000,v1=") and header.count("v1=") == 2
    assert verify(header, body, ["new-secret"], now=1_000_100) == 1_000_000
    assert verify(header, body, ["old-secret"], now=1_000_100) == 1_000_000
    with pytest.raises(SignatureInvalid) as info:
        verify(header, body, ["other"], now=1_000_100)
    assert info.value.code == "bad_signature"
    with pytest.raises(SignatureInvalid) as info:
        verify(header, body, ["new-secret"], now=1_000_000 + 301)
    assert info.value.code == "stale"
    with pytest.raises(SignatureInvalid) as info:
        verify(header, b'{"id": "e2"}', ["new-secret"], now=1_000_100)
    assert info.value.code == "bad_signature"
    for bad in (None, "", "v1=abc", "t=notanumber,v1=abc"):
        with pytest.raises(SignatureInvalid):
            verify(bad, body, ["new-secret"], now=1_000_100)


# --- subscriptions --------------------------------------------------------------


def test_create_validates_events_and_url(session, make_application):
    acme = make_application("acme")
    with pytest.raises(InvalidWebhook):
        create_subscription(session, acme, url=URL, events=["domain.exploded"], allow_private=True)
    with pytest.raises(InvalidWebhook):
        create_subscription(session, acme, url=URL, events=[], allow_private=True)
    with pytest.raises(InvalidWebhook, match="https"):
        create_subscription(
            session, acme, url="http://hooks.acme.example/x", events=["domain.ready"]
        )
    with pytest.raises(InvalidWebhook, match="not deliverable"):
        create_subscription(session, acme, url="https://localhost/x", events=["domain.ready"])
    with pytest.raises(InvalidWebhook, match="credentials"):
        create_subscription(
            session,
            acme,
            url="https://u:p@hooks.acme.example/x",
            events=["domain.ready"],
            allow_private=True,
        )
    subscription, secret = _subscribe(session, acme, ["domain.ready", "domain.ready"])
    assert subscription.events == ["domain.ready"] and secret.startswith("whsec_")
    assert subscription.secret == secret


def test_rotate_keeps_previous_secret_for_grace(session, make_application):
    acme = make_application("acme")
    subscription, old = _subscribe(session, acme)
    t0 = utcnow()
    _, new = rotate_secret(session, acme, subscription.id, grace=timedelta(hours=1), now=t0)
    session.commit()
    assert new != old and subscription.secret == new
    assert signing_secrets(subscription, now=t0 + timedelta(minutes=30)) == [new, old]
    assert signing_secrets(subscription, now=t0 + timedelta(hours=2)) == [new]
    globex = make_application("globex")
    with pytest.raises(WebhookNotFound):
        rotate_secret(session, globex, subscription.id)


# --- outbox ---------------------------------------------------------------------


def test_status_changes_and_deletion_enqueue_snapshots_per_subscription(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    acme_sub, _ = _subscribe(session, acme)
    ready_only, _ = _subscribe(session, acme, ["domain.ready"])
    globex_sub, _ = _subscribe(session, globex)

    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    assert session.query(WebhookDelivery).count() == 0  # pending_dns is not a webhook event

    _make_ready(session, domain)
    rows = session.query(WebhookDelivery).order_by(WebhookDelivery.created_at).all()
    assert {(r.subscription_id, r.event_type) for r in rows} == {
        (acme_sub.id, "domain.ready"),
        (ready_only.id, "domain.ready"),
    }
    payload = rows[0].payload
    assert payload["type"] == "domain.ready" and payload["data"]["domain"]["status"] == "ready"
    assert payload["data"]["domain"]["reference"] == "ws_1"
    assert payload["data"]["domain"]["hostname"] == "forms.customer.example"
    assert [c["status"] for c in payload["data"]["domain"]["checks"]] == ["passing"] * 4
    assert uuid.UUID(payload["id"]) == rows[0].event_id
    assert rows[0].next_attempt_at is not None

    record_check(session, domain, CheckType.ROUTING, CheckStatus.FAILING, error_code="cname_gone")
    session.commit()
    types = sorted(
        r.event_type for r in session.query(WebhookDelivery).filter_by(subscription_id=acme_sub.id)
    )
    assert types == ["domain.attention_required", "domain.ready"]

    record_check(session, domain, CheckType.ROUTING, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.READY, reason="recovered")
    delete_domain(session, acme, domain.id)
    session.commit()
    types = sorted(
        r.event_type for r in session.query(WebhookDelivery).filter_by(subscription_id=acme_sub.id)
    )
    assert types == [
        "domain.attention_required",
        "domain.deleted",
        "domain.ready",
        "domain.recovered",
    ]
    deleted = [
        r
        for r in session.query(WebhookDelivery).filter_by(subscription_id=acme_sub.id)
        if r.event_type == "domain.deleted"
    ][0]
    assert deleted.payload["data"]["domain"]["status"] == "deleting"
    assert deleted.payload["data"]["domain"]["dns_records"] == []

    # The ready-only subscription got one event; globex got none of acme's.
    assert session.query(WebhookDelivery).filter_by(subscription_id=ready_only.id).count() == 1
    assert session.query(WebhookDelivery).filter_by(subscription_id=globex_sub.id).count() == 0


# --- delivery -------------------------------------------------------------------


class Receiver:
    """Local endpoint recording deliveries; status configurable per call."""

    def __init__(self):
        self.received = []
        self.status = 200
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                server.received.append((dict(self.headers), body))
                self.send_response(server.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hooks"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


def test_worker_delivers_signed_payload_over_http(
    session, session_factory, make_application, receiver, monkeypatch
):
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")  # the receiver is plain HTTP on loopback
    acme = make_application("acme")
    subscription, secret = _subscribe(session, acme, url=receiver.url)
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    _make_ready(session, domain)

    result = deliver_due(session_factory)
    assert (result.attempted, result.delivered, result.failed) == (1, 1, 0)
    headers, body = receiver.received[0]
    assert headers["X-Custom-Domain-Event"] == "domain.ready"
    assert verify(headers[HEADER], body, [secret], now=int(time.time()))
    assert json.loads(body)["data"]["domain"]["reference"] == "ws_1"
    assert headers["X-Custom-Domain-Attempt"] == "1"
    session.commit()
    session.commit()  # end any read transaction (SQLite snapshots)
    session.expire_all()
    delivery = session.query(WebhookDelivery).one()
    assert delivery.state == "delivered" and delivery.last_status == 200 and delivery.attempts == 1
    assert deliver_due(session_factory).attempted == 0


def test_failures_back_off_then_abandon_and_replay_recovers(
    session, session_factory, make_application
):
    acme = make_application("acme")
    subscription, _ = _subscribe(session, acme)
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    _make_ready(session, domain)
    delivery = session.query(WebhookDelivery).one()
    calls = []

    def failing(url, body, headers):
        calls.append(headers["X-Custom-Domain-Attempt"])
        return 503, "down"

    t0 = utcnow()
    assert attempt_delivery(session_factory, delivery.id, sender=failing, now=t0) == "retry"
    session.commit()  # end any read transaction (SQLite snapshots)
    session.expire_all()
    assert delivery.attempts == 1 and delivery.next_attempt_at == t0 + BACKOFF[0]
    assert delivery.last_status == 503 and "HTTP 503" in delivery.last_error
    # Not due before the backoff elapses; leases keep two workers apart.
    assert (
        deliver_due(session_factory, sender=failing, now=t0 + timedelta(seconds=30)).attempted == 0
    )
    assert (
        attempt_delivery(
            session_factory, delivery.id, sender=failing, now=t0 + timedelta(seconds=30)
        )
        == "skipped"
    )

    now = t0
    for _ in range(MAX_ATTEMPTS - 1):
        now = now + timedelta(days=2)
        attempt_delivery(session_factory, delivery.id, sender=failing, now=now)
    session.commit()  # end any read transaction (SQLite snapshots)
    session.expire_all()
    assert delivery.state == "abandoned" and delivery.attempts == MAX_ATTEMPTS
    assert len(calls) == MAX_ATTEMPTS

    def exploding(url, body, headers):
        raise ConnectionError("refused")

    replay_delivery(session, acme, subscription.id, delivery.id, now=now)
    session.commit()
    assert attempt_delivery(session_factory, delivery.id, sender=exploding, now=now) == "retry"
    session.commit()  # end any read transaction (SQLite snapshots)
    session.expire_all()
    assert "ConnectionError" in delivery.last_error and delivery.state == "pending"

    def ok(url, body, headers):
        return 204, ""

    assert (
        attempt_delivery(session_factory, delivery.id, sender=ok, now=now + timedelta(days=1))
        == "delivered"
    )
    session.commit()  # end any read transaction (SQLite snapshots)
    session.expire_all()
    assert delivery.state == "delivered"

    assert replay_since(session, acme, subscription.id, t0 - timedelta(days=1), now=now) == 1
    session.commit()
    session.commit()  # end any read transaction (SQLite snapshots)
    session.expire_all()
    assert delivery.state == "pending" and delivery.attempts == 0

    revoke_subscription(session, acme, subscription.id)
    session.commit()
    assert (
        attempt_delivery(session_factory, delivery.id, sender=ok, now=now + timedelta(days=3))
        == "abandoned"
    )


def test_delivery_history_is_application_scoped(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    subscription, _ = _subscribe(session, acme)
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    _make_ready(session, domain)
    assert len(list_deliveries(session, acme, subscription.id)) == 1
    assert len(list_deliveries(session, acme, subscription.id, state="pending")) == 1
    assert list_deliveries(session, acme, subscription.id, state="delivered") == []
    with pytest.raises(WebhookNotFound):
        list_deliveries(session, globex, subscription.id)


def test_worker_loop_runs_and_stops(session_factory):
    worker = WebhookWorker(session_factory, sender=lambda u, b, h: (200, ""))
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop, 60))
    thread.start()
    for _ in range(100):
        if worker.last_result is not None:
            break
        time.sleep(0.02)
    stop.set()
    thread.join(5)
    assert worker.last_result is not None and worker.last_result.attempted == 0


# --- sample consumer ------------------------------------------------------------


def test_sample_consumer_handles_duplicates_and_out_of_order():
    from examples.webhook_consumer import Consumer

    consumer = Consumer(["whsec_test"])

    def delivery(event_id, created_at, status, now):
        body = json.dumps(
            {
                "id": event_id,
                "type": "domain.ready" if status == "ready" else "domain.attention_required",
                "created_at": created_at,
                "data": {"domain": {"id": "d1", "status": status}},
            }
        ).encode()
        return body, sign(body, ["whsec_test"], timestamp=now)

    body1, sig1 = delivery("e1", "2026-09-25T10:00:00+00:00", "ready", 1_000_000)
    body2, sig2 = delivery("e2", "2026-09-25T10:05:00+00:00", "attention_required", 1_000_010)
    assert consumer.handle(body2, sig2, now=1_000_010) == "applied"  # newer arrives first
    assert consumer.handle(body1, sig1, now=1_000_010) == "stale"  # older arrives later
    assert consumer.state["d1"]["status"] == "attention_required"
    assert consumer.handle(body2, sig2, now=1_000_010) == "duplicate"  # retry of the same event
    with pytest.raises(SignatureInvalid):
        consumer.handle(body1, sig2, now=1_000_010)
    with pytest.raises(SignatureInvalid):
        consumer.handle(body1, sig1, now=1_000_000 + 1000)


# --- API ------------------------------------------------------------------------


def test_webhook_endpoints(session, make_application, monkeypatch):
    import os

    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import create_app
    from app.services.applications import issue_credential

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")
    app = create_app()
    from tests.conftest import make_session_factory  # noqa: F401

    factory = session.get_bind()

    def override():
        from app.db.session import make_session_factory as mk

        s = mk(factory)()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    acme = make_application("acme")
    globex = make_application("globex")
    _, acme_secret = issue_credential(session, acme, label="t")
    _, globex_secret = issue_credential(session, globex, label="t")
    session.commit()
    acme_headers = {"Authorization": f"Bearer {acme_secret}"}
    globex_headers = {"Authorization": f"Bearer {globex_secret}"}

    with TestClient(app) as client:
        created = client.post(
            "/v1/webhooks",
            json={"url": "https://localhost/hooks", "events": ["domain.ready"]},
            headers=acme_headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert (
            body["secret"].startswith("whsec_")
            and body["events"] == ["domain.ready"]
            and body["active"]
        )
        webhook_id = body["id"]
        assert "secret" not in client.get("/v1/webhooks", headers=acme_headers).json()[0]
        assert client.get("/v1/webhooks", headers=globex_headers).json() == []

        bad = client.post(
            "/v1/webhooks",
            json={"url": "https://localhost/hooks", "events": ["domain.nope"]},
            headers=acme_headers,
        )
        assert bad.status_code == 422
        assert (
            client.post(
                "/v1/webhooks",
                json={"url": "ftp://x", "events": ["domain.ready"]},
                headers=acme_headers,
            ).status_code
            == 422
        )

        domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
        _make_ready(session, domain)
        history = client.get(f"/v1/webhooks/{webhook_id}/deliveries", headers=acme_headers).json()
        assert (
            len(history) == 1
            and history[0]["state"] == "pending"
            and history[0]["event_type"] == "domain.ready"
        )
        assert (
            client.get(f"/v1/webhooks/{webhook_id}/deliveries", headers=globex_headers).status_code
            == 404
        )

        rotated = client.post(f"/v1/webhooks/{webhook_id}/rotate", headers=acme_headers)
        assert rotated.status_code == 200 and rotated.json()["secret"] != body["secret"]
        assert rotated.json()["previous_secret_expires_at"] is not None

        replayed = client.post(
            f"/v1/webhooks/{webhook_id}/deliveries/{history[0]['id']}/replay", headers=acme_headers
        )
        assert replayed.status_code == 202 and replayed.json()["state"] == "pending"
        since = client.post(
            f"/v1/webhooks/{webhook_id}/replay",
            params={"since": "2020-01-01T00:00:00Z"},
            headers=acme_headers,
        )
        assert since.status_code == 202 and since.json() == {"requeued": 1}

        assert (
            client.delete(f"/v1/webhooks/{webhook_id}", headers=globex_headers).status_code == 404
        )
        revoked = client.delete(f"/v1/webhooks/{webhook_id}", headers=acme_headers)
        assert revoked.status_code == 200 and revoked.json()["active"] is False
        assert (
            client.delete(f"/v1/webhooks/{uuid.uuid4()}", headers=acme_headers).status_code == 404
        )
        spec = client.get("/v1/openapi.json").json()
        assert (
            "/v1/webhooks" in spec["paths"] and "/v1/webhooks/{webhook_id}/replay" in spec["paths"]
        )
    assert os.environ["WEBHOOK_WORKER_ENABLED"] == "false"


def test_delivery_refuses_private_addresses_on_each_attempt(
    session, session_factory, make_application, receiver, monkeypatch
):
    from app.webhooks.worker import http_sender

    acme = make_application("acme")
    subscription, _ = _subscribe(session, acme, url=receiver.url)  # 127.0.0.1, allowed at creation
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    _make_ready(session, domain)
    delivery = session.query(WebhookDelivery).one()

    # Without the private allowance the attempt is refused before any packet is sent.
    monkeypatch.delenv("ORIGIN_ALLOW_PRIVATE", raising=False)
    assert attempt_delivery(session_factory, delivery.id, sender=http_sender) == "retry"
    session.commit()
    session.expire_all()
    assert "InvalidWebhook" in delivery.last_error and receiver.received == []

    # A trusted self-hosted deployment may deliver to private addresses.
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")
    replay_delivery(session, acme, subscription.id, delivery.id)
    session.commit()
    assert attempt_delivery(session_factory, delivery.id, sender=http_sender) == "delivered"
    assert len(receiver.received) == 1
    assert receiver.received[0][0]["Host"].startswith("127.0.0.1")


def test_http_sender_rejects_rebinding_to_private(monkeypatch):
    from app.services import webhooks as service
    from app.webhooks.worker import http_sender

    monkeypatch.delenv("ORIGIN_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(
        service,
        "resolve_public",
        lambda host, port, allow_private: (_ for _ in ()).throw(
            service.InvalidWebhook("non-public")
        ),
    )
    with pytest.raises(service.InvalidWebhook):
        http_sender("https://hooks.acme.example/x", b"{}", {})


def test_delivery_attempts_take_their_own_time(
    session, session_factory, make_application, monkeypatch
):
    """A slow batch must not sign later deliveries with the batch's start time."""
    from datetime import timedelta

    from sqlalchemy import select as sa_select

    from app.models import WebhookDelivery
    from app.services.webhooks import create_subscription
    from app.webhooks import worker as worker_module
    from app.webhooks.signature import verify

    acme = make_application("acme")
    subscription, secret = create_subscription(
        session, acme, url=URL, events=["domain.ready"], allow_private=True
    )
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    _make_ready(session, domain)
    session.commit()
    stale = utcnow() - timedelta(minutes=10)
    for delivery in session.scalars(sa_select(WebhookDelivery)):
        delivery.next_attempt_at = stale - timedelta(minutes=1)
    session.commit()

    # The batch "started" ten minutes ago (first clock read); each attempt
    # must still take the current time for its lease and signature.
    clock = [stale]
    real_utcnow = worker_module.utcnow

    def batch_clock():
        return clock.pop() if clock else real_utcnow()

    monkeypatch.setattr(worker_module, "utcnow", batch_clock)
    seen = []

    def sender(url, body, headers):
        seen.append((body, headers))
        return 200, "ok"

    result = worker_module.deliver_due(session_factory, sender=sender)
    assert result.delivered == 1 and result.at == stale
    body, headers = seen[0]
    # Verified against the real clock with the consumer's default tolerance.
    assert (
        verify(headers[worker_module.HEADER], body, [secret]) >= int(real_utcnow().timestamp()) - 5
    )
    session.commit()
    session.expire_all()
    delivery = session.scalar(sa_select(WebhookDelivery))
    assert delivery.last_attempt_at >= real_utcnow() - timedelta(seconds=10)


def test_webhook_url_with_invalid_port_is_rejected(session, make_application):
    from app.services.webhooks import InvalidWebhook, validate_url

    with pytest.raises(InvalidWebhook):
        validate_url("https://hooks.example:abc/cd", allow_private=True)
