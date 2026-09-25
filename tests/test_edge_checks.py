from datetime import UTC, datetime, timedelta

import pytest

from app.edge.config import EDGE_HEALTH_VALUE, build_apps, health_route
from app.edge.probe import EdgeProbe, EdgeProbeFailed
from app.edge.settings import EdgeSettings
from app.models import ApplicationStatus, CheckStatus, CheckType, DomainStatus
from app.models.types import utcnow
from app.services.applications import (
    activate_origin,
    record_origin_verification,
    register_origin,
    set_application_status,
)
from app.services.dns_checks import REVALIDATE_INTERVAL
from app.services.domains import (
    claim_domain,
    delete_domain,
    is_serveable,
    mark_claim_verified,
    record_check,
    transition_status,
)
from app.services.edge_checks import (
    CERTIFICATE_EXPIRY_WARNING,
    certificate_authorized,
    certificate_outcome,
    eligible_for_edge_checks,
    origin_outcome,
    run_edge_checks,
)

SETTINGS = EdgeSettings(reconcile_enabled=True, legacy_api_enabled=False)


class FakeProber:
    def __init__(self):
        self.not_after = datetime.now(UTC) + timedelta(days=60)
        self.status = 204
        self.marker = EDGE_HEALTH_VALUE
        self.failure: EdgeProbeFailed | None = None
        self.calls: list[str] = []

    def probe(self, hostname):
        self.calls.append(hostname)
        if self.failure:
            raise self.failure
        return EdgeProbe("203.0.113.10", self.not_after, "Let's Encrypt", self.status, self.marker)


@pytest.fixture
def prober():
    return FakeProber()


@pytest.fixture
def acme(session, make_application):
    application = make_application("acme")
    origin = register_origin(session, application, host="app.acme.example")
    record_origin_verification(session, origin, verified=True)
    activate_origin(session, origin)
    session.commit()
    return application


def _dns_verified(session, application, hostname="forms.customer.example"):
    domain = claim_domain(session, application, hostname, "ws_1")
    mark_claim_verified(session, domain)
    record_check(session, domain, CheckType.OWNERSHIP, CheckStatus.PASSING)
    record_check(session, domain, CheckType.ROUTING, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    session.commit()
    return domain


def test_certificate_authorization_rules(session, acme, make_application):
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    assert not certificate_authorized(None)
    assert not certificate_authorized(domain)  # pending_dns, claim pending
    mark_claim_verified(session, domain)
    assert not certificate_authorized(domain)  # still pending_dns
    transition_status(session, domain, DomainStatus.PROVISIONING)
    assert certificate_authorized(domain)
    transition_status(session, domain, DomainStatus.ATTENTION_REQUIRED)
    assert certificate_authorized(domain)  # renewals continue during drift
    transition_status(session, domain, DomainStatus.SUSPENDED)
    assert not certificate_authorized(domain)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    set_application_status(session, acme, ApplicationStatus.SUSPENDED)
    assert not certificate_authorized(domain)
    set_application_status(session, acme, ApplicationStatus.ACTIVE)
    delete_domain(session, acme, domain.id)
    assert not certificate_authorized(domain)


def test_readiness_requires_probe_and_origin(session, acme, prober):
    domain = _dns_verified(session, acme)
    assert eligible_for_edge_checks(domain)
    t0 = utcnow()
    certificate, origin = run_edge_checks(session, domain, prober, SETTINGS, now=t0)
    session.commit()
    assert certificate.status == CheckStatus.PASSING
    assert (
        certificate.details["issuer"] == "Let's Encrypt"
        and certificate.details["address"] == "203.0.113.10"
    )
    assert certificate.next_check_at == t0 + REVALIDATE_INTERVAL
    assert origin.status == CheckStatus.PASSING and origin.details == {
        "origin": "https://app.acme.example:443",
        "attempts": 0,
    }
    assert domain.status == DomainStatus.READY and is_serveable(domain)
    assert prober.calls == ["forms.customer.example"]


def test_missing_origin_blocks_readiness(session, make_application, prober):
    bare = make_application("bare")
    domain = _dns_verified(session, bare)
    certificate, origin = run_edge_checks(session, domain, prober, SETTINGS)
    assert certificate.status == CheckStatus.PASSING
    assert origin.error_code == "origin_not_ready" and "origin verify" in origin.message
    assert domain.status == DomainStatus.PROVISIONING


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (
            lambda p: setattr(p, "failure", EdgeProbeFailed("tls_handshake_failed", "x")),
            "tls_handshake_failed",
        ),
        (lambda p: setattr(p, "marker", None), "edge_not_reached"),
        (lambda p: setattr(p, "status", 200), "edge_not_reached"),
        (
            lambda p: setattr(p, "not_after", datetime.now(UTC) + CERTIFICATE_EXPIRY_WARNING / 2),
            "certificate_expiring",
        ),
        (
            lambda p: setattr(p, "not_after", datetime.now(UTC) - timedelta(days=1)),
            "certificate_expired",
        ),
    ],
)
def test_certificate_failures_have_codes_and_backoff(session, acme, prober, setup, code):
    domain = _dns_verified(session, acme)
    setup(prober)
    t0 = utcnow()
    certificate, _ = run_edge_checks(session, domain, prober, SETTINGS, now=t0)
    assert certificate.status == CheckStatus.FAILING and certificate.error_code == code
    assert certificate.next_check_at == t0 + timedelta(minutes=1)
    assert domain.status == DomainStatus.PROVISIONING


def test_ready_domain_drops_and_recovers_on_probe(session, acme, prober):
    domain = _dns_verified(session, acme)
    run_edge_checks(session, domain, prober, SETTINGS)
    assert domain.status == DomainStatus.READY

    prober.failure = EdgeProbeFailed("connection_failed", "edge down")
    run_edge_checks(session, domain, prober, SETTINGS)
    session.commit()
    assert domain.status == DomainStatus.ATTENTION_REQUIRED and not is_serveable(domain)

    prober.failure = None
    run_edge_checks(session, domain, prober, SETTINGS)
    session.commit()
    assert domain.status == DomainStatus.READY and is_serveable(domain)


def test_https_disabled_marks_certificate_as_not_applicable(session, acme, prober):
    domain = _dns_verified(session, acme)
    local = EdgeSettings(disable_https=True, reconcile_enabled=True, legacy_api_enabled=False)
    outcome = certificate_outcome(prober, domain, local)
    assert outcome.passing and outcome.details == {"tls": "disabled"} and prober.calls == []


def test_origin_outcome_reflects_serving_origin(session, acme):
    domain = _dns_verified(session, acme)
    assert origin_outcome(domain).passing
    record_origin_verification(session, acme.active_origin, verified=False, error_code="x")
    session.commit()
    session.expire_all()
    assert origin_outcome(domain).error_code == "origin_not_ready"


def test_apps_config_has_health_route_and_on_demand_tls(session, acme):
    apps = build_apps(session, SETTINGS)
    routes = apps["http"]["servers"]["edge"]["routes"]
    assert routes[0] == health_route()
    assert routes[0]["handle"][0]["handler"] == "static_response"
    tls = apps["tls"]["automation"]
    assert tls["on_demand"]["permission"] == {"module": "http", "endpoint": SETTINGS.ask_url}
    assert tls["policies"] == [{"on_demand": True, "issuers": [{"module": "acme"}]}]
    with_email = EdgeSettings(
        acme_email="ops@example.net", reconcile_enabled=True, legacy_api_enabled=False
    )
    assert build_apps(session, with_email)["tls"]["automation"]["policies"][0]["issuers"] == [
        {"module": "acme", "email": "ops@example.net"}
    ]
    local = EdgeSettings(disable_https=True, reconcile_enabled=True, legacy_api_enabled=False)
    assert "tls" not in build_apps(session, local)


def test_edge_settings_probe_options():
    settings = EdgeSettings.from_env(
        {
            "ENABLE_LEGACY_API": "false",
            "EDGE_ASK_URL": "http://api:9000/internal/tls/ask",
            "EDGE_ASK_TRUSTED_HOSTS": "10.0.0.5, 10.0.0.6",
            "EDGE_PROBE_ADDRESS": "edge.internal:8443",
            "EDGE_PROBE_CA_FILE": "/etc/ssl/edge-ca.pem",
            "EDGE_PROBE_TIMEOUT": "20",
        }
    )
    assert settings.ask_url == "http://api:9000/internal/tls/ask"
    assert settings.ask_trusted_hosts == ("10.0.0.5", "10.0.0.6")
    assert settings.probe_address == "edge.internal:8443" and settings.probe_timeout == 20
    from app.edge.settings import EdgeConfigurationError

    with pytest.raises(EdgeConfigurationError, match="host:port"):
        EdgeSettings.from_env({"ENABLE_LEGACY_API": "false", "EDGE_PROBE_ADDRESS": "edge"})
