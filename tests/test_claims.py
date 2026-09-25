import threading

import pytest

from app.hostname import InvalidHostname
from app.models import (
    ApplicationStatus,
    CheckStatus,
    CheckType,
    ClaimStatus,
    Domain,
    DomainStatus,
    EventType,
)
from app.models.types import utcnow
from app.services.applications import set_application_status
from app.services.domains import (
    CHALLENGE_LABEL,
    claim_domain,
    delete_domain,
    find_live_by_hostname,
    is_serveable,
    mark_claim_verified,
    record_check,
    reissue_claim,
    transition_status,
)
from app.services.errors import (
    ApplicationSuspended,
    HostnameAlreadyClaimed,
    InvalidReference,
    InvalidStatusTransition,
)


def test_claim_creates_domain_claim_checks_and_events(session, make_application):
    acme = make_application("acme")
    domain = claim_domain(
        session, acme, "Forms.Customer.Example.", "ws_123", metadata={"plan": "pro"}
    )
    session.commit()

    assert domain.hostname == "forms.customer.example"
    assert domain.reference == "ws_123"
    assert domain.status == DomainStatus.PENDING_DNS
    assert domain.extra == {"plan": "pro"}
    assert domain.deleted_at is None

    claim = domain.active_claim
    assert claim is not None
    assert claim.status == ClaimStatus.PENDING
    assert claim.txt_record_name == f"{CHALLENGE_LABEL}.forms.customer.example"
    assert claim.txt_record_value == f"custom-domain-verify={claim.token}"
    assert len(claim.token) >= 40
    assert claim.cname_target == "acme.edge.example.net"

    assert {c.check_type for c in domain.checks} == set(CheckType)
    assert all(c.status == CheckStatus.PENDING for c in domain.checks)

    event_types = [e.event_type for e in domain.events]
    assert event_types == [EventType.DOMAIN_CREATED, EventType.CLAIM_ISSUED]
    assert all(e.application_id == acme.id for e in domain.events)


def test_hostname_variants_collapse_to_one_claim(session, make_application):
    acme = make_application("acme")
    claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    with pytest.raises(HostnameAlreadyClaimed):
        claim_domain(session, acme, "FORMS.customer.example.", "ws_2")


def test_hostname_cannot_be_claimed_by_another_application(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    with pytest.raises(HostnameAlreadyClaimed):
        claim_domain(session, globex, "forms.customer.example", "ws_other")
    # The session is still usable after the failed claim.
    other = claim_domain(session, globex, "other.customer.example", "ws_other")
    session.commit()
    assert other.id is not None


def test_invalid_hostname_and_reference_are_rejected(session, make_application):
    acme = make_application("acme")
    with pytest.raises(InvalidHostname):
        claim_domain(session, acme, "customer.example", "ws_1")
    with pytest.raises(InvalidReference):
        claim_domain(session, acme, "forms.customer.example", "  ")
    with pytest.raises(InvalidReference):
        claim_domain(session, acme, "forms.customer.example", "x" * 256)


def test_suspended_application_cannot_claim(session, make_application):
    acme = make_application("acme")
    set_application_status(session, acme, ApplicationStatus.SUSPENDED)
    session.commit()
    with pytest.raises(ApplicationSuspended):
        claim_domain(session, acme, "forms.customer.example", "ws_1")


def test_reclaim_after_delete_creates_fresh_row_and_token(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    first = claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    first_id, first_token = first.id, first.active_claim.token

    delete_domain(session, acme, first.id)
    session.commit()

    second = claim_domain(session, globex, "forms.customer.example", "ws_g")
    session.commit()

    assert second.id != first_id
    assert second.application_id == globex.id
    assert second.active_claim.token != first_token
    assert second.active_claim.status == ClaimStatus.PENDING

    live = find_live_by_hostname(session, "forms.customer.example")
    assert live is not None and live.id == second.id

    tombstone = session.get(Domain, first_id)
    assert tombstone.deleted_at is not None
    assert tombstone.active_claim is None
    assert all(c.status == ClaimStatus.REVOKED for c in tombstone.claims)


def test_reissue_claim_revokes_old_token(session, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    old = domain.active_claim

    new = reissue_claim(session, acme, domain.id)
    session.commit()
    session.refresh(domain)

    assert new.id != old.id
    assert new.token != old.token
    assert old.status == ClaimStatus.REVOKED
    assert old.revoked_at is not None
    assert domain.active_claim.id == new.id
    assert [c.status for c in domain.claims] == [ClaimStatus.REVOKED, ClaimStatus.PENDING]


def _make_ready(session, domain):
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    assert is_serveable(domain)


def test_reissue_on_ready_domain_resets_status_and_checks(session, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    _make_ready(session, domain)

    new = reissue_claim(session, acme, domain.id)
    session.commit()
    session.refresh(domain)

    assert domain.status == DomainStatus.PENDING_DNS
    assert not is_serveable(domain)
    assert all(c.status == CheckStatus.PENDING for c in domain.checks)
    assert domain.active_claim.id == new.id and new.status == ClaimStatus.PENDING

    # Verifying the new token alone must not restore service.
    mark_claim_verified(session, domain)
    session.commit()
    assert not is_serveable(domain)
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, domain, DomainStatus.READY)

    # Only fresh passing checks and an explicit readiness transition do.
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    assert is_serveable(domain)

    reasons = [
        e.payload.get("reason") for e in domain.events if e.event_type == EventType.STATUS_CHANGED
    ]
    assert "claim_reissued" in reasons


def test_reissue_on_suspended_domain_keeps_suspension(session, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    transition_status(session, domain, DomainStatus.SUSPENDED)
    session.commit()

    reissue_claim(session, acme, domain.id)
    session.commit()
    session.refresh(domain)
    assert domain.status == DomainStatus.SUSPENDED
    assert not is_serveable(domain)


def test_concurrent_claims_only_one_wins(session_factory, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    applications = [acme, globex]
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        application = applications[index % len(applications)]
        with session_factory() as s:
            app_row = s.get(type(application), application.id)
            # End the read transaction so the claim starts a fresh write
            # transaction; SQLite rejects writes on a stale read snapshot.
            s.commit()
            barrier.wait()
            try:
                claim_domain(s, app_row, "race.customer.example", f"ws_{index}", now=utcnow())
                s.commit()
                result = "won"
            except HostnameAlreadyClaimed:
                s.rollback()
                result = "lost"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(outcomes) == workers
    assert outcomes.count("won") == 1
    assert outcomes.count("lost") == workers - 1
    with session_factory() as s:
        rows = s.query(Domain).filter(Domain.hostname == "race.customer.example").all()
        assert len(rows) == 1
        assert len(rows[0].claims) == 1
