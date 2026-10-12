"""Connect sessions and the customer's setup page (/connect/<token>).

Builds on the Domain Connect fixtures: a domain whose provider supports the
template (``connectable``), a throwaway local signing key (``local_signer``)
and a public edge address (``with_return``). No real network.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import timedelta
from urllib.parse import parse_qsl, urlsplit

import pytest
from sqlalchemy import select, update

from app.models import (
    Application,
    ApplicationStatus,
    CheckStatus,
    CheckType,
    ConnectSession,
    Domain,
    DomainCheck,
    DomainEvent,
    DomainStatus,
)
from app.models.types import utcnow
from app.services import applications as app_service
from app.services import connect_sessions, domain_connect, secret_box
from app.services import domains as domain_service
from tests import test_domain_connect, test_v1_api
from tests.test_domain_connect import SECRET, SYNC_UX, _fresh, _signed_part, _verify
from tests.test_v1_api import _error

# Fixtures shared with the Domain Connect tests.
make_client = test_v1_api.make_client
rsa_key = test_domain_connect.rsa_key
connectable = test_domain_connect.connectable
local_signer = test_domain_connect.local_signer
no_signer = test_domain_connect.no_signer
with_return = test_domain_connect.with_return

TOKEN_RE = re.compile(r"^https://edge\.example\.net/connect/([A-Za-z0-9_-]{43})$")


@pytest.fixture
def client(make_client, with_return):
    with make_client(legacy=False) as client:
        yield client


def _create(client, domain, headers, **body):
    response = client.post(
        f"/v1/domains/{domain.id}/connect-session", headers=headers, json=body or None
    )
    assert response.status_code == 201, response.text
    return response.json()


def _path(url: str) -> str:
    return urlsplit(url).path


def _token(url: str) -> str:
    match = TOKEN_RE.match(url)
    assert match, url
    return match.group(1)


def _csrf(client, page: str) -> str:
    response = client.get(page)
    assert response.status_code == 200, response.text
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


# --- POST /v1/domains/{id}/connect-session ------------------------------------------


def test_a_session_is_a_url_with_a_token_stored_only_as_a_hash(client, connectable, session):
    _, domain, headers = connectable
    before = utcnow()
    body = _create(client, domain, headers)
    assert set(body) == {"id", "url", "expires_at", "created_at"}
    token = _token(body["url"])
    _fresh(session)
    row = session.get(ConnectSession, uuid.UUID(body["id"]))
    assert row.domain_id == domain.id
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    for value in (row.token_hash, row.app_name, row.return_url, str(row.id)):
        assert token not in (value or "")
    assert (
        before + timedelta(hours=24) <= row.expires_at <= utcnow() + timedelta(hours=24, seconds=5)
    )
    # Every session is a new random token.
    assert _token(_create(client, domain, headers)["url"]) != token


def test_ttl_name_and_return_url(client, connectable, session):
    _, domain, headers = connectable
    body = _create(
        client,
        domain,
        headers,
        ttl_seconds=600,
        app_name="Acme Forms",
        return_url="https://app.example/settings",
    )
    _fresh(session)
    row = session.get(ConnectSession, uuid.UUID(body["id"]))
    assert row.expires_at - row.created_at == timedelta(seconds=600)
    assert (row.app_name, row.return_url) == ("Acme Forms", "https://app.example/settings")


@pytest.mark.parametrize(
    "body",
    [
        {"ttl_seconds": 299},
        {"ttl_seconds": 86401},
        {"app_name": "line\nbreak"},
        {"app_name": "x" * 65},
        {"app_name": "   "},
        {"return_url": "http://app.example/"},
        {"return_url": "https://app.example/#frag"},
        {"return_url": "javascript:alert(1)"},
        {"unknown": "field"},
    ],
)
def test_requests_are_validated(client, connectable, body):
    _, domain, headers = connectable
    response = client.post(f"/v1/domains/{domain.id}/connect-session", headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert _error(response)["code"] == "validation_error"


def test_without_a_public_edge_address_there_is_no_page(make_client, connectable):
    _, domain, headers = connectable
    with make_client(legacy=False) as client:
        response = client.post(f"/v1/domains/{domain.id}/connect-session", headers=headers)
    assert response.status_code == 409
    assert _error(response)["code"] == "connect_page_unavailable"


def test_read_only_keys_and_other_applications_are_refused(
    client, connectable, session, make_application
):
    application, domain, _ = connectable
    _, read = app_service.issue_credential(session, application, label="dash", scope="read")
    other = make_application("globex")
    _, other_key = app_service.issue_credential(session, other, label="x")
    session.commit()
    path = f"/v1/domains/{domain.id}/connect-session"
    response = client.post(path, headers={"Authorization": f"Bearer {read}"})
    assert response.status_code == 403
    assert _error(response)["code"] == "credential_read_only"
    # Another application's domain is not found, never forbidden.
    response = client.post(path, headers={"Authorization": f"Bearer {other_key}"})
    assert response.status_code == 404
    assert _error(response)["code"] == "domain_not_found"
    assert session.scalar(select(ConnectSession.id)) is None


def test_sessions_are_rate_limited_per_domain_and_application(
    client, connectable, session, monkeypatch
):
    application, domain, headers = connectable
    monkeypatch.setattr(connect_sessions, "DOMAIN_MAX_PER_WINDOW", 2)
    for _ in range(2):
        _create(client, domain, headers)
    response = client.post(f"/v1/domains/{domain.id}/connect-session", headers=headers)
    assert response.status_code == 429
    assert _error(response)["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0

    monkeypatch.setattr(connect_sessions, "DOMAIN_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(connect_sessions, "APPLICATION_MAX_PER_WINDOW", 3)
    second = domain_service.claim_domain(session, application, "shop.customer.example", "ws_2")
    session.commit()
    _create(client, second, headers)
    response = client.post(f"/v1/domains/{second.id}/connect-session", headers=headers)
    assert response.status_code == 429
    assert "per application" in _error(response)["message"]


# --- revocation and the uniform not-found page ---------------------------------------


def test_revocation(client, connectable, session, make_application):
    application, domain, headers = connectable
    body = _create(client, domain, headers)
    page = _path(body["url"])
    assert client.get(page).status_code == 200
    path = f"/v1/domains/{domain.id}/connect-session/{body['id']}"

    # Only through the domain it belongs to, and only by its application.
    second = domain_service.claim_domain(session, application, "shop.customer.example", "ws_2")
    other = make_application("globex")
    _, other_key = app_service.issue_credential(session, other, label="x")
    session.commit()
    wrong_domain = client.delete(
        f"/v1/domains/{second.id}/connect-session/{body['id']}", headers=headers
    )
    assert wrong_domain.status_code == 404
    assert _error(wrong_domain)["code"] == "connect_session_not_found"
    foreign = client.delete(path, headers={"Authorization": f"Bearer {other_key}"})
    assert foreign.status_code == 404
    assert _error(foreign)["code"] == "domain_not_found"
    unknown = client.delete(
        f"/v1/domains/{domain.id}/connect-session/{uuid.uuid4()}", headers=headers
    )
    assert _error(unknown)["code"] == "connect_session_not_found"
    assert client.get(page).status_code == 200

    response = client.delete(path, headers=headers)
    assert response.status_code == 204 and response.content == b""
    assert client.get(page).status_code == 404
    assert client.delete(path, headers=headers).status_code == 204  # again: no change
    _fresh(session)
    assert session.get(ConnectSession, uuid.UUID(body["id"])).revoked_at is not None


def test_every_invalid_link_gets_the_same_404(client, connectable, session):
    application, domain, headers = connectable
    pages = {}
    for name in ("expired", "revoked", "domain_deleted", "app_suspended", "valid"):
        pages[name] = _path(_create(client, domain, headers)["url"])
    revoked = session.scalars(select(ConnectSession).order_by(ConnectSession.created_at)).all()
    session.execute(
        update(ConnectSession)
        .where(ConnectSession.id == revoked[0].id)
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    session.execute(
        update(ConnectSession).where(ConnectSession.id == revoked[1].id).values(revoked_at=utcnow())
    )
    session.commit()
    assert client.get(pages["valid"]).status_code == 200

    answers = [
        client.get("/connect/" + "A" * 43),  # well formed, never issued
        client.get("/connect/short"),
        client.get("/connect/" + "A" * 300),
        client.get(pages["expired"]),
        client.get(pages["revoked"]),
        client.post("/connect/" + "A" * 43 + "/check", data={"csrf": "x"}),
        client.post(pages["revoked"] + "/apply", data={"csrf": "x"}),
    ]
    # The application suspended, then the domain deleted.
    session.execute(
        update(Application)
        .where(Application.id == application.id)
        .values(status=ApplicationStatus.SUSPENDED)
    )
    session.commit()
    answers.append(client.get(pages["app_suspended"]))
    session.execute(
        update(Application)
        .where(Application.id == application.id)
        .values(status=ApplicationStatus.ACTIVE)
    )
    session.commit()
    assert client.get(pages["valid"]).status_code == 200
    domain_service.delete_domain(session, session.get(Application, application.id), domain.id)
    session.commit()
    answers.append(client.get(pages["domain_deleted"]))

    assert {answer.status_code for answer in answers} == {404}
    assert len({answer.text for answer in answers}) == 1
    assert "Link not valid" in answers[0].text
    for answer in answers:
        assert answer.headers["Referrer-Policy"] == "no-referrer"
        assert "set-cookie" not in answer.headers


# --- the page ------------------------------------------------------------------------


def test_the_page_with_one_step_setup(client, connectable, local_signer, session):
    _, domain, headers = connectable
    claim = domain.active_claim
    url = _create(client, domain, headers)["url"]
    page = _path(url)
    response = client.get(page)
    assert response.status_code == 200
    html = response.text
    assert domain.hostname in html
    assert "Waiting for DNS" in html and "Cloudflare" in html
    assert claim.txt_record_value in html and claim.cname_target in html
    assert "_custom-domain-challenge.forms" in html  # the name relative to the zone
    assert "Set up automatically with Cloudflare" in html
    assert f'action="{page}/apply"' in html and f'action="{page}/check"' in html
    assert f'<meta http-equiv="refresh" content="30; url={page}">' in html
    assert "<script" not in html.lower()
    for check in ("Ownership", "Routing", "HTTPS certificate", "Origin"):
        assert check in html
    # Nothing about the application or the service's operator.
    assert "acme" not in html.lower().replace("acme.", "")
    assert "edge.example.net" in html  # the neutral brand: the edge hostname

    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp
    assert "form-action 'self' https://dc.dns.example;" in csp
    assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("cd_connect=")
    assert f"Path={page}" in cookie and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()


def test_the_page_with_the_provider_guide(client, connectable, no_signer):
    _, domain, headers = connectable
    page = _path(_create(client, domain, headers)["url"])
    response = client.get(page)
    html = response.text
    assert "Set up automatically" not in html
    assert "Add the two records below at Cloudflare." in html
    assert re.search(r'<a href="[^"]*cloudflare[^"]*" rel="noopener noreferrer">', html)
    assert "Cloudflare setup guide" in html
    assert "form-action 'self';" in response.headers["Content-Security-Policy"]


def test_a_ready_domain_stops_refreshing(client, connectable, local_signer, session):
    _, domain, headers = connectable
    page = _path(_create(client, domain, headers)["url"])
    session.execute(update(Domain).where(Domain.id == domain.id).values(status=DomainStatus.READY))
    session.commit()
    html = client.get(page).text
    assert "Connected" in html
    assert "http-equiv" not in html and "Set up automatically" not in html


def test_customer_controlled_values_are_escaped(client, connectable, session):
    _, domain, headers = connectable
    page = _path(
        _create(
            client,
            domain,
            headers,
            app_name='<img src=x onerror="1">',
            return_url="https://app.example/back?a=1&b=<2>",
        )["url"]
    )
    session.execute(
        update(DomainCheck)
        .where(DomainCheck.domain_id == domain.id, DomainCheck.check_type == CheckType.OWNERSHIP)
        .values(
            status=CheckStatus.FAILING,
            error_code="txt_token_mismatch",
            message="The TXT record has another value",
            details={"observed": ["<script>alert(1)</script>"]},
        )
    )
    session.commit()
    html = client.get(page, params={"n": "<b>injected</b>"}).text
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img" not in html and "Back to &lt;img src=x onerror=" in html
    assert 'href="https://app.example/back?a=1&amp;b=&lt;2&gt;"' in html
    assert "injected" not in html  # notices are fixed messages picked by a code
    assert "Wrong value" in html


def test_check_now_needs_the_csrf_value_and_is_rate_limited(client, connectable, session):
    _, domain, headers = connectable
    page = _path(_create(client, domain, headers)["url"])
    later = utcnow() + timedelta(hours=1)
    session.execute(update(DomainCheck).values(next_check_at=later))
    session.commit()

    # Without the page's CSRF value nothing happens.
    refused = client.post(page + "/check", data={"csrf": "A" * 43}, follow_redirects=False)
    assert refused.status_code == 403 and "Nothing was changed" in refused.text
    csrf = _csrf(client, page)
    client.cookies.clear()
    assert (
        client.post(page + "/check", data={"csrf": csrf}, follow_redirects=False).status_code == 403
    )
    _fresh(session)
    assert all(c.next_check_at == later for c in session.scalars(select(DomainCheck)))

    csrf = _csrf(client, page)
    response = client.post(page + "/check", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{page}?n=checking"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    _fresh(session)
    now = utcnow()
    assert all(c.next_check_at <= now for c in session.scalars(select(DomainCheck)))
    event = session.scalars(
        select(DomainEvent).where(DomainEvent.event_type == "domain.recheck_requested")
    ).one()
    assert event.payload == {"requested_by": "connect_page"}
    assert "Checking again now" in client.get(f"{page}?n=checking").text

    again = client.post(page + "/check", data={"csrf": csrf}, follow_redirects=False)
    assert again.headers["location"] == f"{page}?n=rate_limited"


def _sealed(state: str) -> dict:
    data = secret_box.unseal(state, domain_connect.STATE_PURPOSE)
    assert data is not None
    return data


def test_apply_redirects_to_a_signed_url_and_returns_to_the_page(
    client, connectable, local_signer, session, caplog
):
    from app.observability import install_log_redaction

    install_log_redaction()
    _, domain, headers = connectable
    with caplog.at_level(logging.DEBUG):
        url = _create(client, domain, headers)["url"]
        token, page = _token(url), _path(url)
        refused = client.post(page + "/apply", data={"csrf": "A" * 43}, follow_redirects=False)
        assert refused.status_code == 403
        csrf = _csrf(client, page)
        response = client.post(page + "/apply", data={"csrf": csrf}, follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith(f"{SYNC_UX}/v2/domainTemplates/providers/")
        assert response.headers["Referrer-Policy"] == "no-referrer"
        params = dict(parse_qsl(urlsplit(location).query))
        _verify(local_signer, _signed_part(location), params["sig"])
        assert params["redirect_uri"] == "https://edge.example.net/v1/domain-connect/return"
        # The page's address (with its token) never reaches the provider readable.
        assert token not in location
        sealed = _sealed(params["state"])
        assert "r" not in sealed and token not in str(sealed)

        back = client.get(
            "/v1/domain-connect/return", params={"state": params["state"]}, follow_redirects=False
        )
        assert back.status_code == 303
        assert back.headers["location"] == f"{url}?n=returned"
        declined = client.get(
            "/v1/domain-connect/return",
            params={"state": params["state"], "error": "access_denied"},
            follow_redirects=False,
        )
        assert declined.headers["location"] == f"{url}?n=returned&error=access_denied"
        html = client.get(f"{page}?n=returned&error=access_denied").text
        assert "Your DNS provider made no changes." in html
        assert "is applying the records" in client.get(f"{page}?n=returned").text
        assert client.get("/connect/" + "B" * 43).status_code == 404

    # The token is never logged; the access log line is masked.
    assert token not in caplog.text
    from app.observability import redact

    assert (
        redact(f'"GET /connect/{token}?n=1 HTTP/1.1" 200') == '"GET /connect/***?n=1 HTTP/1.1" 200'
    )
    _fresh(session)
    events = session.scalars(
        select(DomainEvent).where(DomainEvent.event_type == "domain.connect_url_issued")
    ).all()
    assert len(events) == 1
    assert session.get(Domain, domain.id).connect_started_at is not None


def test_apply_without_a_secret_key_never_sends_the_page_address(
    client, connectable, local_signer, monkeypatch
):
    monkeypatch.delenv("SERVICE_SECRET_KEY", raising=False)
    _, domain, headers = connectable
    url = _create(client, domain, headers)["url"]
    page = _path(url)
    csrf = _csrf(client, page)
    response = client.post(page + "/apply", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert _token(url) not in location
    assert [k for k, _ in parse_qsl(urlsplit(location).query)] == [
        "domain",
        "host",
        "target",
        "token",
        "key",
        "sig",
    ]


def test_apply_when_one_step_setup_is_not_available(client, connectable, no_signer):
    _, domain, headers = connectable
    page = _path(_create(client, domain, headers)["url"])
    csrf = _csrf(client, page)
    response = client.post(page + "/apply", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{page}?n=unavailable"
    assert "Automatic setup is not available" in client.get(response.headers["location"]).text


def test_apply_is_rate_limited_like_the_api(client, connectable, local_signer, monkeypatch):
    _, domain, headers = connectable
    monkeypatch.setattr(domain_connect, "DOMAIN_MAX_PER_WINDOW", 1)
    page = _path(_create(client, domain, headers)["url"])
    csrf = _csrf(client, page)
    first = client.post(page + "/apply", data={"csrf": csrf}, follow_redirects=False)
    assert first.headers["location"].startswith(SYNC_UX)
    second = client.post(page + "/apply", data={"csrf": csrf}, follow_redirects=False)
    assert second.headers["location"] == f"{page}?n=busy"


def test_the_return_state_secret_is_bound_to_the_service_key(monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    sealed = secret_box.seal(
        {
            "d": str(uuid.uuid4()),
            "e": int((utcnow() + timedelta(minutes=5)).timestamp()),
            "x": "not encrypted with this key",
        },
        domain_connect.STATE_PURPOSE,
    )
    opened = domain_connect.open_return_state(sealed)
    assert opened is not None and opened.redirect_uri is None


def test_sessions_go_with_their_domain_when_it_is_purged(client, connectable, session):
    application, domain, headers = connectable
    _create(client, domain, headers)
    domain_service.delete_domain(session, application, domain.id)
    session.commit()
    assert session.scalar(select(ConnectSession.id)) is not None  # kept with the tombstone
    session.execute(
        update(Domain).where(Domain.id == domain.id).values(purge_after=utcnow() - timedelta(1))
    )
    session.commit()
    assert domain_service.purge_tombstones(session) == 1
    session.commit()
    assert session.scalar(select(ConnectSession.id)) is None


def test_guessing_tokens_is_limited_per_client_address(client, connectable):
    from fastapi.testclient import TestClient

    from app.v1.throttle import FailedAuthLimiter

    _, domain, headers = connectable
    page = _path(_create(client, domain, headers)["url"])
    client.app.state.v1_auth_limiter = FailedAuthLimiter(max_failures=3)
    edge = TestClient(client.app, client=("127.0.0.1", 1000))  # a trusted edge peer
    attacker = {"X-Forwarded-For": "8.8.8.8"}
    for _ in range(3):
        assert edge.get("/connect/" + "A" * 43, headers=attacker).status_code == 404
    refused = edge.get(page, headers=attacker)  # even a working link, from that address
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1
    assert "Too many attempts" in refused.text
    assert edge.get(page, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
