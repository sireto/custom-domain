"""Domain Connect apply URLs: signing, the connect endpoint, the signer, the return.

No real network: keys are generated here, the remote signer is an
``httpx.MockTransport``, and discovery results are written to ``dns_zones``
directly.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.dns import signing
from app.dns.settings import SetupSettings, SignerSettings
from app.models import (
    Application,
    CheckType,
    DnsZone,
    Domain,
    DomainCheck,
    DomainConnectSigner,
    DomainEvent,
    DomainStatus,
)
from app.models.types import utcnow
from app.services import applications as app_service
from app.services import dns_setup, domain_connect, secret_box
from app.services import domains as domain_service
from app.services.dns_checks import Outcome, apply_dns_outcomes
from app.services.errors import DomainConnectUnavailable, InvalidSignerConfig
from tests import test_v1_api
from tests.test_operator_api import AUTH, make_app

make_client = test_v1_api.make_client
_error = test_v1_api._error

PROVIDER = "provider.example"
SYNC_UX = "https://dc.dns.example/manage"
SECRET = "s" * 40
CREDENTIAL = "dcs_" + "A" * 43
SIGNER_URL = "https://signer.example/internal/domain-connect/sign"
TEMPLATE = SetupSettings(template_provider_id=PROVIDER, template_service_id="hostname")


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64_der(key) -> str:
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.b64encode(der).decode()


def _verify(key, query: str, sig: str) -> None:
    key.public_key().verify(
        base64.b64decode(sig), query.encode(), padding.PKCS1v15(), hashes.SHA256()
    )


@pytest.fixture
def local_signer(monkeypatch, rsa_key):
    """Domain Connect with a local key; the template is provider.example/hostname."""
    monkeypatch.setattr(
        dns_setup, "setup_settings", lambda: SetupSettings(PROVIDER, "hostname", True)
    )
    monkeypatch.setattr(
        domain_connect,
        "signer_settings",
        lambda: SignerSettings("local", private_key=_b64_der(rsa_key)),
    )
    return rsa_key


@pytest.fixture
def no_signer(monkeypatch):
    monkeypatch.setattr(dns_setup, "setup_settings", lambda: TEMPLATE)
    monkeypatch.setattr(domain_connect, "signer_settings", lambda: SignerSettings())


@pytest.fixture
def connectable(session, make_application):
    """An application with a key, and a domain whose provider supports the template."""
    application = make_application("acme")
    _, key = app_service.issue_credential(session, application, label="backend")
    domain = domain_service.claim_domain(session, application, "forms.customer.example", "ws_1")
    session.add(
        DnsZone(
            zone="customer.example",
            provider="cloudflare",
            nameservers=["ns1.cloudflare.com"],
            domain_connect={"provider_id": "cloudflare.com", "url_sync_ux": SYNC_UX},
            template=f"{PROVIDER}/hostname",
            template_supported=True,
            checked_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    domain.zone = "customer.example"
    session.commit()
    return application, domain, {"Authorization": f"Bearer {key}"}


def _fresh(session) -> None:
    """See what other sessions committed: end the snapshot and forget loaded rows."""
    session.rollback()
    session.expire_all()


def _params(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def _signed_part(url: str) -> str:
    """The query exactly as sent, without ``&key=...&sig=...``."""
    query = urlsplit(url).query
    return query[: query.index("&key=")]


# --- the URL and the local signer ----------------------------------------------------


def test_canonical_query_encodes_as_rfc_3986():
    query = signing.canonical_query(
        [("domain", "customer.example"), ("redirect_uri", "https://a.example/x?y=1 2&z=~")]
    )
    assert (
        query
        == "domain=customer.example&redirect_uri=https%3A%2F%2Fa.example%2Fx%3Fy%3D1%202%26z%3D~"
    )


def test_apply_url_shape_with_sig_last():
    signature = signing.Signature(key="_dcpubkeyv1", sig="ab+/c==")
    url = signing.apply_url(SYNC_UX + "/", PROVIDER, "hostname", "domain=x.example", signature)
    assert url == (
        f"{SYNC_UX}/v2/domainTemplates/providers/{PROVIDER}/services/hostname/apply"
        "?domain=x.example&key=_dcpubkeyv1&sig=ab%2B%2Fc%3D%3D"
    )
    assert "ab+/c" not in repr(signature)


def test_local_signature_verifies_and_the_first_key_signs(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = signing.parse_private_keys(
        f"_dcpubkeyv2:{_b64_der(rsa_key)}, _dcpubkeyv1:{_b64_der(other)}"
    )
    assert [k.host for k in keys] == ["_dcpubkeyv2", "_dcpubkeyv1"]
    signature = signing.LocalSigner(keys).sign("domain=a.example", application="a", domain_id="1")
    assert signature.key == "_dcpubkeyv2"
    _verify(rsa_key, "domain=a.example", signature.sig)

    single = signing.parse_private_keys(_b64_der(rsa_key))
    assert single[0].host == "_dcpubkeyv1"
    assert signing.parse_private_keys(_b64_der(rsa_key), "_dcpubkeyv3")[0].host == "_dcpubkeyv3"


def test_bad_keys_are_refused_without_echoing_them():
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    secret_looking = _b64_der(small)
    for value in (secret_looking, "bm90IGEga2V5", "host-only:" + secret_looking[:40]):
        with pytest.raises(signing.InvalidSigningKey) as caught:
            signing.parse_private_keys(value)
        assert secret_looking[:20] not in str(caught.value)
    with pytest.raises(signing.InvalidSigningKey, match="leave DOMAIN_CONNECT_KEY_HOST unset"):
        signing.parse_private_keys("_a:x,_b:y", "_dcpubkeyv1")


def test_settings_validate_and_hide_the_key(rsa_key, tmp_path):
    der = _b64_der(rsa_key)
    settings = SignerSettings.from_env(
        {"DOMAIN_CONNECT_SIGNER": "local", "DOMAIN_CONNECT_PRIVATE_KEY": der}
    )
    assert settings.enabled and der[:30] not in repr(settings)
    key_file = tmp_path / "key"
    key_file.write_text(der + "\n")
    assert SignerSettings.from_env(
        {"DOMAIN_CONNECT_SIGNER": "local", "DOMAIN_CONNECT_PRIVATE_KEY_FILE": str(key_file)}
    ).enabled
    remote = SignerSettings.from_env(
        {
            "DOMAIN_CONNECT_SIGNER": "remote",
            "DOMAIN_CONNECT_SIGNER_URL": SIGNER_URL,
            "DOMAIN_CONNECT_SIGNER_CREDENTIAL": CREDENTIAL,
        }
    )
    assert remote.enabled and CREDENTIAL not in repr(remote)
    assert not SignerSettings.from_env({}).enabled
    for env in (
        {"DOMAIN_CONNECT_SIGNER": "maybe"},
        {"DOMAIN_CONNECT_SIGNER": "local"},
        {"DOMAIN_CONNECT_SIGNER": "local", "DOMAIN_CONNECT_PRIVATE_KEY": "bm9wZQ"},
        {"DOMAIN_CONNECT_SIGNER": "remote", "DOMAIN_CONNECT_SIGNER_URL": "http://x.example"},
        {
            "DOMAIN_CONNECT_SIGNER": "remote",
            "DOMAIN_CONNECT_SIGNER_URL": SIGNER_URL,
            "DOMAIN_CONNECT_SIGNER_CREDENTIAL": "short",
        },
        {"DOMAIN_CONNECT_RETURN_URL": "http://insecure.example/back"},
    ):
        with pytest.raises(ValueError):
            SignerSettings.from_env(env)
    # signing_enabled follows the signer, and needs a template id.
    env = {"DOMAIN_CONNECT_SIGNER": "local", "DOMAIN_CONNECT_PRIVATE_KEY": der}
    assert not SetupSettings.from_env(env).signing_enabled
    assert SetupSettings.from_env({**env, "DOMAIN_CONNECT_PROVIDER_ID": PROVIDER}).signing_enabled


def test_public_key_records_split_in_200_character_parts(rsa_key):
    records = signing.public_key_records(rsa_key)
    assert records[0].startswith("p=1,a=RS256,d=") and len(records) == 2
    data = "".join(r.split(",d=", 1)[1] for r in records)
    der = rsa_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert base64.b64decode(data) == der


# --- the remote signer ---------------------------------------------------------------


def _remote(handler) -> signing.RemoteSigner:
    return signing.RemoteSigner(
        SIGNER_URL,
        CREDENTIAL,
        provider_id=PROVIDER,
        service_id="hostname",
        transport=httpx.MockTransport(handler),
    )


def test_remote_signer_sends_exactly_the_contract():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"sig": "c2lnbmF0dXJl", "key": "_dcpubkeyv2"})

    signature = _remote(handler).sign("domain=a.example&host=x", application="acme", domain_id="d1")
    assert signature == signing.Signature(key="_dcpubkeyv2", sig="c2lnbmF0dXJl")
    (request,) = seen
    assert request.method == "POST" and str(request.url) == SIGNER_URL
    assert request.headers["Authorization"] == f"Bearer {CREDENTIAL}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {
        "provider_id": PROVIDER,
        "service_id": "hostname",
        "application": "acme",
        "domain_id": "d1",
        "query": "domain=a.example&host=x",
    }
    assert CREDENTIAL not in repr(_remote(handler))


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (400, "bad_request", "refused"),
        (400, "bad_query", "refused"),
        (401, "unauthorized", "refused"),
        (403, "foreign_template", "refused"),
        (403, "redirect_not_https", "refused"),
        (403, "wrong_target", "refused"),
        (403, "unknown_domain", "refused"),
        (403, "host_mismatch", "refused"),
        (403, "domain_not_live", "refused"),
        (403, "wrong_token", "refused"),
        (403, "something_new", "refused"),
        (429, "rate_limited", "rate_limited"),
        (503, "verification_unavailable", "unavailable"),
        (500, None, "unavailable"),
        (302, None, "unavailable"),
    ],
)
def test_remote_refusals_are_mapped(status, code, kind):
    def handler(request):
        body = {"error": {"code": code, "message": "no"}} if code else {}
        headers = {"Retry-After": "120"} if status == 429 else {}
        return httpx.Response(status, json=body, headers=headers)

    with pytest.raises(signing.SigningFailed) as caught:
        _remote(handler).sign("q", application="a", domain_id="d")
    assert caught.value.kind == kind
    if code in signing.REMOTE_REFUSALS:
        assert caught.value.code == code
    if status == 429:
        assert caught.value.retry_after == 120


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda request: httpx.Response(200, json={"sig": "not base64!", "key": "_k"}),
        lambda request: httpx.Response(200, json={"sig": "YQ==", "key": "bad key"}),
        lambda request: httpx.Response(200, text="<html>"),
    ],
)
def test_unusable_remote_answers_are_unavailable(answer):
    with pytest.raises(signing.SigningFailed) as caught:
        _remote(answer).sign("q", application="a", domain_id="d")
    assert caught.value.kind == "unavailable"


# --- POST /v1/domains/{id}/connect ---------------------------------------------------


def test_connect_returns_a_signed_url_with_sig_last(
    make_client, connectable, local_signer, session, caplog
):
    from app.observability import install_log_redaction

    install_log_redaction()
    _, domain, headers = connectable
    claim = domain.active_claim
    with caplog.at_level(logging.DEBUG), make_client(legacy=False) as client:
        response = client.post(
            f"/v1/domains/{domain.id}/connect",
            headers=headers,
            json={"redirect_uri": "https://app.example/done?x=1", "state": "s 1/é"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"url", "provider", "expires_at"}
    assert body["provider"] == {"id": "cloudflare", "name": "Cloudflare"}
    url = body["url"]
    assert url.startswith(
        f"{SYNC_UX}/v2/domainTemplates/providers/{PROVIDER}/services/hostname/apply?"
    )
    params = _params(url)
    assert [name for name, _ in params] == [
        "domain",
        "host",
        "target",
        "token",
        "redirect_uri",
        "state",
        "key",
        "sig",
    ]
    values = dict(params)
    assert values["domain"] == "customer.example" and values["host"] == "forms"
    assert values["target"] == claim.cname_target and values["token"] == claim.token
    # Without a return address the application's values go to the provider as given.
    assert values["redirect_uri"] == "https://app.example/done?x=1"
    assert values["state"] == "s 1/é"
    assert "state=s%201%2F%C3%A9" in url  # RFC 3986, never "+"
    assert values["key"] == "_dcpubkeyv1"
    _verify(local_signer, _signed_part(url), values["sig"])
    assert url.endswith("&sig=" + quote(values["sig"], safe=""))
    # The audit line names the domain; never the key, the signature or the token.
    assert f"domain connect link issued: application acme domain {domain.id}" in caplog.text
    for secret in (_b64_der(local_signer)[:40], values["sig"][:40], claim.token):
        assert secret not in caplog.text

    _fresh(session)
    stored = session.get(Domain, domain.id)
    assert stored.connect_started_at is not None
    events = session.scalars(
        select(DomainEvent).where(DomainEvent.event_type == "domain.connect_url_issued")
    ).all()
    assert len(events) == 1
    assert events[0].payload == {"provider": "cloudflare", "signer": "local"}
    assert values["sig"] not in json.dumps(events[0].payload)
    soon = utcnow() + domain_connect.FAST_RECHECK_INTERVAL + timedelta(seconds=5)
    for check in stored.checks:
        if check.check_type in (CheckType.OWNERSHIP, CheckType.ROUTING):
            assert check.next_check_at <= soon


def test_connect_without_a_body(make_client, connectable, local_signer):
    _, domain, headers = connectable
    with make_client(legacy=False) as client:
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers)
    assert response.status_code == 200, response.text
    assert [n for n, _ in _params(response.json()["url"])] == [
        "domain",
        "host",
        "target",
        "token",
        "key",
        "sig",
    ]


def test_409_when_one_step_setup_is_not_available(
    make_client, connectable, no_signer, session, make_application
):
    _, domain, headers = connectable
    with make_client(legacy=False) as client:
        # The provider supports the template, but nothing can sign.
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers)
        assert response.status_code == 409
        assert _error(response)["code"] == "domain_connect_unavailable"
        assert (
            client.get(f"/v1/domains/{domain.id}", headers=headers).json()["setup"]["method"]
            == "guided"
        )
        # Another application's domain is not found, never forbidden.
        other = make_application("globex")
        _, other_key = app_service.issue_credential(session, other, label="x")
        session.commit()
        response = client.post(
            f"/v1/domains/{domain.id}/connect",
            headers={"Authorization": f"Bearer {other_key}"},
        )
        assert response.status_code == 404


def test_409_for_an_unsupported_provider_or_a_suspended_domain(
    make_client, connectable, local_signer, session
):
    _, domain, headers = connectable
    session.execute(update(DnsZone).values(template_supported=False))
    session.commit()
    with make_client(legacy=False) as client:
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers)
        assert response.status_code == 409
        assert _error(response)["details"] == {"method": "guided"}
        session.execute(update(DnsZone).values(template_supported=True))
        session.execute(
            update(Domain).where(Domain.id == domain.id).values(status=DomainStatus.SUSPENDED)
        )
        session.commit()
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers)
        assert response.status_code == 409
        assert _error(response)["details"] == {"status": "suspended"}


@pytest.mark.parametrize(
    "body",
    [
        {"redirect_uri": "http://app.example/done"},
        {"redirect_uri": "https://user:pw@app.example/done"},
        {"redirect_uri": "https://user@app.example/done"},
        {"redirect_uri": "https://app.example/done#frag"},
        {"redirect_uri": "https:///no-host"},
        {"redirect_uri": "https://app.example/" + "a" * 600},
        {"state": "line\nbreak"},
        {"state": "x" * 513},
        {"unknown": "field"},
    ],
)
def test_redirect_uri_and_state_are_validated(make_client, connectable, local_signer, body):
    _, domain, headers = connectable
    with make_client(legacy=False) as client:
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert _error(response)["code"] == "validation_error"


def test_read_only_keys_are_refused(make_client, connectable, local_signer, session):
    application, domain, _ = connectable
    _, read = app_service.issue_credential(session, application, label="dash", scope="read")
    session.commit()
    with make_client(legacy=False) as client:
        response = client.post(
            f"/v1/domains/{domain.id}/connect", headers={"Authorization": f"Bearer {read}"}
        )
    assert response.status_code == 403
    assert _error(response)["code"] == "credential_read_only"


def test_links_are_rate_limited_per_domain(make_client, connectable, local_signer):
    _, domain, headers = connectable
    with make_client(legacy=False) as client:
        for _ in range(domain_connect.DOMAIN_MAX_PER_WINDOW):
            assert (
                client.post(f"/v1/domains/{domain.id}/connect", headers=headers).status_code == 200
            )
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers)
    assert response.status_code == 429
    assert _error(response)["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0


def test_the_per_application_limit(session, connectable, local_signer, monkeypatch):
    application, domain, _ = connectable
    monkeypatch.setattr(domain_connect, "APPLICATION_MAX_PER_WINDOW", 2)
    for _ in range(2):
        domain_service.record_event(session, domain, "domain.connect_url_issued", {})
    session.commit()
    from app.services.errors import RateLimited

    with pytest.raises(RateLimited, match="per application"):
        domain_connect.prepare_connect(session, application, domain.id)


# --- the remote signer through the API -----------------------------------------------


@pytest.fixture
def remote_signer(session, monkeypatch):
    """A remote signer set through the operator API's service function."""
    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    monkeypatch.setattr(dns_setup, "setup_settings", lambda: SetupSettings())
    monkeypatch.setattr(domain_connect, "signer_settings", lambda: SignerSettings())
    domain_connect.set_remote_signer(
        session, url=SIGNER_URL, credential=CREDENTIAL, provider_id=PROVIDER, service_id="hostname"
    )
    session.commit()


def test_the_signer_reads_the_domain_back_while_the_edge_waits(
    make_client, connectable, remote_signer, session, session_factory, caplog
):
    """The signer calls GET /v1/domains/{id} mid-request: no lock may be held."""
    from app.observability import install_log_redaction

    install_log_redaction()
    application, domain, headers = connectable
    _, portal_key = app_service.issue_credential(session, application, label="portal", scope="read")
    session.commit()
    holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        client = holder["client"]
        # What the signing service does: read the domain with its own key.
        seen = client.get(
            f"/v1/domains/{body['domain_id']}", headers={"Authorization": f"Bearer {portal_key}"}
        )
        assert seen.status_code == 200
        record = seen.json()
        query = dict(parse_qsl(body["query"]))
        assert f"{query['host']}.{query['domain']}" == record["hostname"]
        assert query["target"] == record["dns_records"][1]["value"]
        assert f"custom-domain-verify={query['token']}" == record["dns_records"][0]["value"]
        # And a write to the application row, which the edge's lock would block.
        with session_factory() as other:
            other.execute(
                update(Application)
                .where(Application.id == application.id)
                .values(updated_at=Application.updated_at)
            )
            other.commit()
        holder["query"] = body["query"]
        return httpx.Response(200, json={"sig": "c2lnbmF0dXJlPT0rLw==", "key": "_dcpubkeyv1"})

    with caplog.at_level(logging.DEBUG), make_client(legacy=False) as client:
        holder["client"] = client
        client.app.state.domain_connect_transport = httpx.MockTransport(handler)
        assert (
            client.get(f"/v1/domains/{domain.id}", headers=headers).json()["setup"]["method"]
            == "domain_connect"
        )
        response = client.post(
            f"/v1/domains/{domain.id}/connect", headers=headers, json={"state": "abc"}
        )
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    assert _signed_part(url) == holder["query"]
    assert url.endswith("&key=_dcpubkeyv1&sig=c2lnbmF0dXJlPT0rLw%3D%3D")
    logged = caplog.text
    claim_token = domain.active_claim.token
    for secret in (CREDENTIAL, "c2lnbmF0dXJlPT0rLw", claim_token, holder["query"]):
        assert secret not in logged


@pytest.mark.parametrize(
    ("status", "code", "expected", "expected_code"),
    [
        (403, "wrong_token", 409, "domain_connect_unavailable"),
        (401, "unauthorized", 409, "domain_connect_unavailable"),
        (429, "rate_limited", 429, "rate_limited"),
        (503, "verification_unavailable", 503, "domain_connect_signer_unavailable"),
        (502, None, 503, "domain_connect_signer_unavailable"),
    ],
)
def test_signer_refusals_reach_the_application(
    make_client, connectable, remote_signer, session, status, code, expected, expected_code
):
    _, domain, headers = connectable

    def handler(request):
        body = {"error": {"code": code, "message": "no"}} if code else {}
        return httpx.Response(status, json=body, headers={"Retry-After": "30"})

    with make_client(legacy=False) as client:
        client.app.state.domain_connect_transport = httpx.MockTransport(handler)
        response = client.post(f"/v1/domains/{domain.id}/connect", headers=headers)
    assert response.status_code == expected, response.text
    error = _error(response)
    assert error["code"] == expected_code
    if expected == 409:
        assert error["details"] == {"reason": code}
    if expected == 429:
        assert response.headers["Retry-After"] == "30"
    # A refused link is not recorded as issued.
    assert not session.scalars(
        select(DomainEvent).where(DomainEvent.event_type == "domain.connect_url_issued")
    ).all()


# --- the operator API ----------------------------------------------------------------


@pytest.fixture
def operator(session_factory, session, monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    monkeypatch.setattr(domain_connect, "signer_settings", lambda: SignerSettings())
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        yield client


SIGNER_PATH = "/operator/v1/domain-connect/signer"
BODY = {
    "signer": "remote",
    "url": SIGNER_URL,
    "credential": CREDENTIAL,
    "provider_id": PROVIDER,
    "service_id": "hostname",
}


def test_operator_signer_get_put_delete(operator, session, caplog):
    from app.observability import install_log_redaction

    install_log_redaction()
    assert operator.get(SIGNER_PATH).status_code == 401
    empty = operator.get(SIGNER_PATH, headers=AUTH)
    assert empty.status_code == 200
    assert empty.json() == {
        "signer": None,
        "url": None,
        "credential_prefix": None,
        "provider_id": None,
        "service_id": None,
        "updated_at": None,
    }
    with caplog.at_level(logging.DEBUG):
        put = operator.put(SIGNER_PATH, headers=AUTH, json=BODY)
    assert put.status_code == 200, put.text
    shown = put.json()
    assert shown["signer"] == "remote" and shown["url"] == SIGNER_URL
    assert shown["credential_prefix"] == CREDENTIAL[:12] == "dcs_AAAAAAAA"
    assert shown["provider_id"] == PROVIDER and shown["updated_at"]
    assert CREDENTIAL not in put.text and CREDENTIAL not in caplog.text
    assert operator.get(SIGNER_PATH, headers=AUTH).json() == shown

    _fresh(session)
    row = session.get(DomainConnectSigner, "active")
    assert (
        row.credential_ciphertext.startswith("v1:") and CREDENTIAL not in row.credential_ciphertext
    )
    assert (
        secret_box.decrypt(row.credential_ciphertext, domain_connect.CREDENTIAL_PURPOSE)
        == CREDENTIAL
    )

    # Replaced in place: one row, the new credential.
    other = "dcs_" + "B" * 43
    assert (
        operator.put(SIGNER_PATH, headers=AUTH, json={**BODY, "credential": other}).json()[
            "credential_prefix"
        ]
        == "dcs_BBBBBBBB"
    )
    _fresh(session)
    assert len(session.scalars(select(DomainConnectSigner)).all()) == 1

    deleted = operator.delete(SIGNER_PATH, headers=AUTH)
    assert deleted.status_code == 200 and deleted.json()["signer"] is None
    _fresh(session)
    assert session.scalars(select(DomainConnectSigner)).all() == []


@pytest.mark.parametrize(
    "change",
    [
        {"signer": "local"},
        {"url": "http://signer.example/sign"},
        {"url": "https://user@signer.example/sign"},
        {"credential": "dcs_short"},
        {"credential": "cd_" + "A" * 44},
        {"provider_id": "has space"},
        {"service_id": "a/b"},
        {"extra": "field"},
    ],
)
def test_operator_signer_input_is_validated(operator, change):
    response = operator.put(SIGNER_PATH, headers=AUTH, json={**BODY, **change})
    assert response.status_code == 422, response.text
    assert CREDENTIAL not in response.text


def test_without_service_secret_key_nothing_is_stored(operator, monkeypatch, session):
    monkeypatch.delenv("SERVICE_SECRET_KEY")
    response = operator.put(SIGNER_PATH, headers=AUTH, json=BODY)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_secret_key_missing"
    assert session.scalars(select(DomainConnectSigner)).all() == []


def test_a_credential_that_no_longer_decrypts_is_reported_as_not_held(operator, monkeypatch):
    assert operator.put(SIGNER_PATH, headers=AUTH, json=BODY).status_code == 200
    monkeypatch.setenv("SERVICE_SECRET_KEY", "t" * 40)
    shown = operator.get(SIGNER_PATH, headers=AUTH).json()
    assert shown["signer"] == "remote" and shown["credential_prefix"] is None


def test_the_settings_signer_is_shown_without_details(operator, monkeypatch):
    monkeypatch.setattr(
        domain_connect,
        "signer_settings",
        lambda: SignerSettings("remote", url=SIGNER_URL, credential=CREDENTIAL),
    )
    shown = operator.get(SIGNER_PATH, headers=AUTH).json()
    assert shown["signer"] == "remote" and shown["url"] is None
    assert shown["credential_prefix"] is None


def test_the_operator_signer_switches_setup_to_domain_connect(session, connectable, monkeypatch):
    _, domain, _ = connectable
    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    monkeypatch.setattr(dns_setup, "setup_settings", lambda: SetupSettings())
    monkeypatch.setattr(domain_connect, "signer_settings", lambda: SignerSettings())
    with pytest.raises(InvalidSignerConfig):
        domain_connect.set_remote_signer(
            session, url="ftp://x", credential=CREDENTIAL, provider_id=PROVIDER, service_id="h"
        )
    assert dns_setup.domain_setup(domain).method == "guided"
    domain_connect.set_remote_signer(
        session, url=SIGNER_URL, credential=CREDENTIAL, provider_id=PROVIDER, service_id="hostname"
    )
    assert dns_setup.domain_setup(domain).method == "domain_connect"
    domain_connect.reset_signer(session)
    assert dns_setup.domain_setup(domain).method == "guided"
    session.rollback()


# --- the return and the recheck ------------------------------------------------------


@pytest.fixture
def with_return(monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    monkeypatch.setenv("PUBLIC_API", "true")
    monkeypatch.setenv("EDGE_HOSTNAME", "edge.example.net")


def test_the_return_is_sealed_and_triggers_a_recheck(
    make_client, connectable, local_signer, with_return, session
):
    _, domain, headers = connectable
    with make_client(legacy=False) as client:
        response = client.post(
            f"/v1/domains/{domain.id}/connect",
            headers=headers,
            json={"redirect_uri": "https://app.example/done?x=1", "state": "app-state"},
        )
        assert response.status_code == 200, response.text
        values = dict(_params(response.json()["url"]))
        assert values["redirect_uri"] == "https://edge.example.net/v1/domain-connect/return"
        sealed = values["state"]
        assert "app-state" not in sealed  # base64url, not readable in the URL as is

        # Make the checks not due, as if they had just run.
        later = utcnow() + timedelta(hours=1)
        session.execute(update(DomainCheck).values(next_check_at=later))
        session.commit()

        back = client.get(
            "/v1/domain-connect/return",
            params={"state": sealed},
            follow_redirects=False,
        )
        assert back.status_code == 303
        assert back.headers["location"] == "https://app.example/done?x=1&state=app-state"
        assert back.headers["Referrer-Policy"] == "no-referrer"
        _fresh(session)
        now = utcnow()
        for check in session.scalars(select(DomainCheck).where(DomainCheck.domain_id == domain.id)):
            if check.check_type in (CheckType.OWNERSHIP, CheckType.ROUTING):
                assert check.next_check_at <= now
            else:
                assert check.next_check_at == later

        denied = client.get(
            "/v1/domain-connect/return",
            params={"state": sealed, "error": "access_denied"},
            follow_redirects=False,
        )
        assert denied.headers["location"].endswith("&state=app-state&error=access_denied")
        odd = client.get(
            "/v1/domain-connect/return",
            params={"state": sealed, "error": "<script>"},
            follow_redirects=False,
        )
        assert odd.headers["location"].endswith("&state=app-state")


def test_forged_or_expired_returns_are_refused(make_client, connectable, with_return, session):
    _, domain, _ = connectable
    with make_client(legacy=False) as client:
        forged = client.get(
            "/v1/domain-connect/return",
            params={"state": "eyJkIjoiMSJ9.AAAA"},
            follow_redirects=False,
        )
        assert forged.status_code == 400 and "location" not in forged.headers
        assert "default-src 'none'" in forged.headers["Content-Security-Policy"]
        expired = secret_box.seal(
            {
                "d": str(domain.id),
                "e": int((utcnow() - timedelta(minutes=1)).timestamp()),
                "r": "https://app.example/",
                "s": None,
            },
            domain_connect.STATE_PURPOSE,
        )
        response = client.get(
            "/v1/domain-connect/return", params={"state": expired}, follow_redirects=False
        )
        assert response.status_code == 400 and "location" not in response.headers
        # Without a redirect_uri the customer sees a short page.
        plain = secret_box.seal(
            {"d": str(domain.id), "e": int((utcnow() + timedelta(minutes=5)).timestamp())},
            domain_connect.STATE_PURPOSE,
        )
        page = client.get("/v1/domain-connect/return", params={"state": plain})
        assert page.status_code == 200 and "close this window" in page.text
        # A domain that no longer exists is not an error for the customer.
        gone = secret_box.seal(
            {"d": str(uuid.uuid4()), "e": int((utcnow() + timedelta(minutes=5)).timestamp())},
            domain_connect.STATE_PURPOSE,
        )
        assert client.get("/v1/domain-connect/return", params={"state": gone}).status_code == 200
    _fresh(session)
    assert session.get(Domain, domain.id).connect_started_at is not None


def _failing(code: str) -> Outcome:
    from app.models import CheckStatus

    return Outcome(CheckStatus.FAILING, code, "not yet", {})


def test_failing_checks_run_often_after_a_connect(session, connectable):
    _, domain, _ = connectable
    now = utcnow()
    # Several failures in: normally the backoff is long.
    for check in domain.checks:
        check.details = {"attempts": 5}
    domain.connect_started_at = now - timedelta(minutes=5)
    apply_dns_outcomes(session, domain, _failing("txt_missing"), _failing("cname_missing"), now=now)
    for check_type in (CheckType.OWNERSHIP, CheckType.ROUTING):
        assert domain.check(check_type).next_check_at == now + domain_connect.FAST_RECHECK_INTERVAL

    domain.connect_started_at = now - domain_connect.FAST_RECHECK_WINDOW - timedelta(seconds=1)
    apply_dns_outcomes(session, domain, _failing("txt_missing"), _failing("cname_missing"), now=now)
    assert domain.check(CheckType.OWNERSHIP).next_check_at >= now + timedelta(minutes=30)
    session.rollback()


def test_redirects_keep_the_application_query():
    assert (
        domain_connect.back_to_application("https://a.example/p?q=1", "s t", None)
        == "https://a.example/p?q=1&state=s%20t"
    )
    assert domain_connect.back_to_application("https://a.example/p", None, None) == (
        "https://a.example/p"
    )


def test_prepare_refuses_without_a_signer(session, connectable, no_signer):
    application, domain, _ = connectable
    with pytest.raises(DomainConnectUnavailable):
        domain_connect.prepare_connect(session, application, domain.id)


# --- CLI and portal ------------------------------------------------------------------


def test_cli_sets_shows_and_resets_the_signer(
    session_factory, session, monkeypatch, capsys, rsa_key
):
    import io

    from app import cli

    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    monkeypatch.setattr(domain_connect, "signer_settings", lambda: SignerSettings())
    monkeypatch.setattr(cli, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr("sys.stdin", io.StringIO(CREDENTIAL + "\n"))
    assert (
        cli.main(
            [
                "domain-connect",
                "set-remote",
                "--url",
                SIGNER_URL,
                "--provider-id",
                PROVIDER,
                "--service-id",
                "hostname",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "signer: remote (from the operator)" in out and "dcs_AAAAAAAA..." in out
    assert CREDENTIAL not in out
    assert cli.main(["domain-connect", "signer"]) == 0
    assert SIGNER_URL in capsys.readouterr().out
    assert cli.main(["domain-connect", "reset"]) == 0
    assert "signer: none" in capsys.readouterr().out

    assert cli.main(["domain-connect", "public-key"]) == 2
    monkeypatch.setattr(
        domain_connect,
        "signer_settings",
        lambda: SignerSettings("local", private_key=_b64_der(rsa_key)),
    )
    assert cli.main(["domain-connect", "public-key"]) == 0
    printed = capsys.readouterr().out
    assert "# _dcpubkeyv1." in printed and "p=1,a=RS256,d=" in printed
    assert _b64_der(rsa_key)[:40] not in printed


def test_portal_sets_and_resets_the_signer(portal, monkeypatch, session):
    from tests.test_portal import csrf_of, sign_in

    monkeypatch.setenv("SERVICE_SECRET_KEY", SECRET)
    monkeypatch.setattr(domain_connect, "signer_settings", lambda: SignerSettings())
    sign_in(portal)
    page = portal.get("/portal/edge")
    assert page.status_code == 200 and "Domain Connect signer" in page.text
    form = {k: v for k, v in BODY.items() if k != "signer"}
    refused = portal.post("/portal/edge/domain-connect", data={**form, "csrf": "wrong"})
    assert refused.status_code == 403
    saved = portal.post(
        "/portal/edge/domain-connect",
        data={**form, "csrf": csrf_of(portal)},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/portal/edge?ok=signer_set"
    page = portal.get("/portal/edge?ok=signer_set")
    assert "dcs_AAAAAAAA" in page.text and CREDENTIAL not in page.text
    assert "Remote signing service" in page.text
    bad = portal.post(
        "/portal/edge/domain-connect",
        data={**form, "credential": "nope", "csrf": csrf_of(portal)},
    )
    assert bad.status_code == 400 and "credential must be" in bad.text
    reset = portal.post(
        "/portal/edge/domain-connect/reset",
        data={"csrf": csrf_of(portal)},
        follow_redirects=False,
    )
    assert reset.headers["location"] == "/portal/edge?ok=signer_reset"
    assert session.scalars(select(DomainConnectSigner)).all() == []
