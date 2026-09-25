from datetime import timedelta

import pytest

from app.dns.resolver import DnsNameNotFound, DnsUnavailable, cname_chain
from app.models import CheckStatus, CheckType, ClaimStatus, DomainStatus, EventType
from app.models.types import utcnow
from app.services.dns_checks import (
    BACKOFF,
    OWNERSHIP_LOSS_GRACE,
    REVALIDATE_INTERVAL,
    apply_dns_outcomes,
    ownership_outcome,
    routing_outcome,
    run_dns_checks,
)
from app.services.domains import (
    claim_domain,
    delete_domain,
    is_serveable,
    record_check,
    reissue_claim,
    transition_status,
)

TARGET = "acme.edge.example.net"


class FakeResolver:
    """In-memory DNS: TXT and CNAME per name; NXDOMAIN for unknown names."""

    def __init__(self):
        self.txt_records: dict[str, list[str]] = {}
        self.cnames: dict[str, str] = {}
        self.addresses: set[str] = set()
        self.unavailable: set[str] = set()
        self.queries: list[tuple[str, str]] = []

    def _known(self, name):
        if name in self.unavailable:
            raise DnsUnavailable("timed out")
        return name in self.txt_records or name in self.cnames or name in self.addresses

    def txt(self, name):
        self.queries.append(("TXT", name))
        if not self._known(name):
            raise DnsNameNotFound(name)
        return list(self.txt_records.get(name, []))

    def cname(self, name):
        self.queries.append(("CNAME", name))
        if not self._known(name):
            raise DnsNameNotFound(name)
        return self.cnames.get(name)

    def has_address(self, name):
        return name in self.addresses


@pytest.fixture
def resolver():
    return FakeResolver()


@pytest.fixture
def domain(session, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    return domain


def _publish(resolver, domain, *, txt=True, cname=True, via=None):
    claim = domain.active_claim
    if txt:
        resolver.txt_records[claim.txt_record_name] = [claim.txt_record_value]
    if cname:
        if via:
            resolver.cnames[domain.hostname] = via
            resolver.cnames[via] = TARGET
        else:
            resolver.cnames[domain.hostname] = TARGET


def test_both_records_verify_claim_and_move_to_provisioning(session, resolver, domain):
    _publish(resolver, domain)
    t0 = utcnow()
    ownership, routing = run_dns_checks(session, domain, resolver, now=t0)
    session.commit()
    assert ownership.status == CheckStatus.PASSING and routing.status == CheckStatus.PASSING
    assert ownership.next_check_at == t0 + REVALIDATE_INTERVAL
    assert routing.details == {"chain": [TARGET], "attempts": 0}
    assert domain.active_claim.status == ClaimStatus.VERIFIED
    assert domain.active_claim.verification_method == "dns_txt"
    assert domain.status == DomainStatus.PROVISIONING
    assert not is_serveable(domain)  # certificate and origin checks still pending


def test_missing_records_fail_with_backoff(session, resolver, domain):
    t0 = utcnow()
    ownership, routing = run_dns_checks(session, domain, resolver, now=t0)
    assert ownership.error_code == "txt_record_not_found"
    assert routing.error_code == "cname_not_found"
    assert ownership.next_check_at == t0 + BACKOFF[0]
    assert (
        ownership.details["attempts"] == 1 and ownership.details["failing_since"] == t0.isoformat()
    )
    assert domain.status == DomainStatus.PENDING_DNS

    for attempt in range(2, len(BACKOFF) + 3):
        now = t0 + timedelta(hours=attempt)
        ownership, _ = run_dns_checks(session, domain, resolver, now=now)
        expected = BACKOFF[min(attempt, len(BACKOFF)) - 1]
        assert ownership.next_check_at == now + expected, attempt
        assert ownership.details["attempts"] == attempt
        assert ownership.details["failing_since"] == t0.isoformat()


def test_txt_present_but_wrong_or_stale(session, resolver, domain):
    claim = domain.active_claim
    resolver.txt_records[claim.txt_record_name] = [
        "custom-domain-verify=somethingelse",
        "v=spf1 -all",
    ]
    outcome = ownership_outcome(resolver, domain)
    assert outcome.error_code == "txt_token_mismatch"
    assert outcome.details["observed"] == ["custom-domain-verify=somethingelse", "v=spf1 -all"]

    old_value = claim.txt_record_value
    reissue_claim(session, domain.application, domain.id)
    session.commit()
    session.refresh(domain)
    resolver.txt_records[claim.txt_record_name] = [old_value]
    outcome = ownership_outcome(resolver, domain)
    assert outcome.error_code == "txt_token_stale"
    assert "previous registration" in outcome.message

    resolver.txt_records[claim.txt_record_name] = [" " + domain.active_claim.txt_record_value + " "]
    assert ownership_outcome(resolver, domain).passing

    resolver.txt_records[claim.txt_record_name] = []
    assert ownership_outcome(resolver, domain).error_code == "txt_record_not_found"


def test_cname_chain_and_mismatch_diagnostics(session, resolver, domain):
    _publish(resolver, domain, txt=False, via="lb.customer-cdn.example")
    outcome = routing_outcome(resolver, domain)
    assert outcome.passing and outcome.details["chain"] == ["lb.customer-cdn.example", TARGET]

    resolver.cnames[domain.hostname] = "old-host.example"
    resolver.cnames.pop("lb.customer-cdn.example")
    outcome = routing_outcome(resolver, domain)
    assert outcome.error_code == "cname_target_mismatch"
    assert outcome.details["chain"] == ["old-host.example"] and TARGET in outcome.message

    resolver.cnames.pop(domain.hostname)
    resolver.addresses.add(domain.hostname)
    outcome = routing_outcome(resolver, domain)
    assert outcome.error_code == "cname_not_found" and "A or AAAA" in outcome.message

    # Loops and long chains are bounded.
    resolver.addresses.clear()
    resolver.cnames[domain.hostname] = "a.loop.example"
    resolver.cnames["a.loop.example"] = "b.loop.example"
    resolver.cnames["b.loop.example"] = "a.loop.example"
    assert cname_chain(resolver, domain.hostname) == ["a.loop.example", "b.loop.example"]
    assert routing_outcome(resolver, domain).error_code == "cname_target_mismatch"


def test_dns_unavailable_is_a_retryable_failure(session, resolver, domain):
    claim = domain.active_claim
    resolver.unavailable.update({claim.txt_record_name, domain.hostname})
    ownership, routing = run_dns_checks(session, domain, resolver)
    assert ownership.error_code == "dns_timeout" and routing.error_code == "dns_timeout"
    assert domain.status == DomainStatus.PENDING_DNS


def _make_ready(session, resolver, domain, now):
    _publish(resolver, domain)
    run_dns_checks(session, domain, resolver, now=now)
    record_check(session, domain, CheckType.CERTIFICATE, CheckStatus.PASSING, observed_at=now)
    record_check(session, domain, CheckType.ORIGIN, CheckStatus.PASSING, observed_at=now)
    transition_status(session, domain, DomainStatus.READY, now=now)
    session.commit()
    assert is_serveable(domain)


def test_drift_demotes_and_recovery_restores_ready(session, resolver, domain):
    t0 = utcnow()
    _make_ready(session, resolver, domain, t0)

    resolver.cnames[domain.hostname] = "old-host.example"
    run_dns_checks(session, domain, resolver, now=t0 + timedelta(hours=6))
    session.commit()
    assert domain.status == DomainStatus.ATTENTION_REQUIRED
    assert not is_serveable(domain)
    assert domain.check(CheckType.ROUTING).error_code == "cname_target_mismatch"

    resolver.cnames[domain.hostname] = TARGET
    run_dns_checks(session, domain, resolver, now=t0 + timedelta(hours=7))
    session.commit()
    assert domain.status == DomainStatus.READY and is_serveable(domain)
    reasons = [
        e.payload.get("reason") for e in domain.events if e.event_type == EventType.STATUS_CHANGED
    ]
    assert reasons[-1] == "recovered"


def test_lost_ownership_suspends_after_grace_and_resumes_after_reverification(
    session, resolver, domain
):
    t0 = utcnow()
    _make_ready(session, resolver, domain, t0)
    claim = domain.active_claim
    resolver.txt_records[claim.txt_record_name] = []

    run_dns_checks(session, domain, resolver, now=t0 + timedelta(hours=6))
    assert domain.status == DomainStatus.ATTENTION_REQUIRED
    run_dns_checks(
        session, domain, resolver, now=t0 + timedelta(hours=6) + OWNERSHIP_LOSS_GRACE / 2
    )
    assert domain.status == DomainStatus.ATTENTION_REQUIRED
    run_dns_checks(session, domain, resolver, now=t0 + timedelta(hours=6) + OWNERSHIP_LOSS_GRACE)
    session.commit()
    assert domain.status == DomainStatus.SUSPENDED and not is_serveable(domain)

    resolver.txt_records[claim.txt_record_name] = [claim.txt_record_value]
    run_dns_checks(session, domain, resolver, now=t0 + timedelta(days=2))
    session.commit()
    assert domain.status == DomainStatus.PROVISIONING


def test_provisioning_falls_back_to_pending_when_records_disappear(session, resolver, domain):
    _publish(resolver, domain)
    run_dns_checks(session, domain, resolver)
    assert domain.status == DomainStatus.PROVISIONING
    resolver.cnames.pop(domain.hostname)
    run_dns_checks(session, domain, resolver)
    assert domain.status == DomainStatus.PENDING_DNS
    assert domain.active_claim.status == ClaimStatus.VERIFIED  # ownership itself still holds


def test_previous_owners_records_cannot_verify_a_new_claim(session, resolver, make_application):
    acme = make_application("acme")
    globex = make_application("globex", cname_target="globex.edge.example.net")
    first = claim_domain(session, acme, "forms.customer.example", "ws_a")
    session.commit()
    _publish(resolver, first)
    run_dns_checks(session, first, resolver)
    assert first.active_claim.status == ClaimStatus.VERIFIED
    old_txt_name, old_txt_value = (
        first.active_claim.txt_record_name,
        first.active_claim.txt_record_value,
    )

    delete_domain(session, acme, first.id)
    second = claim_domain(session, globex, "forms.customer.example", "ws_g")
    session.commit()
    # The customer left acme's records in place.
    assert resolver.txt_records[old_txt_name] == [old_txt_value]
    ownership, routing = run_dns_checks(session, second, resolver)
    assert ownership.error_code == "txt_token_mismatch"
    assert routing.error_code == "cname_target_mismatch"
    assert second.status == DomainStatus.PENDING_DNS
    assert second.active_claim.status == ClaimStatus.PENDING
    # And nothing revives the deleted registration: it is not checked at all.
    assert first.deleted_at is not None and not is_serveable(first)


def test_apply_outcomes_records_events(session, resolver, domain):
    from app.services.dns_checks import Outcome

    apply_dns_outcomes(
        session,
        domain,
        Outcome(CheckStatus.FAILING, "txt_record_not_found", "no"),
        Outcome(CheckStatus.FAILING, "cname_not_found", "no"),
    )
    types = [e.event_type for e in domain.events]
    assert types.count(EventType.CHECK_UPDATED) == 2
