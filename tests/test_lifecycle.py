from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    ApplicationStatus,
    CheckStatus,
    CheckType,
    ClaimStatus,
    Domain,
    DomainCheck,
    DomainEvent,
    DomainStatus,
    EventType,
    OwnershipClaim,
)
from app.models.types import utcnow
from app.services.applications import set_application_status
from app.services.domains import (
    TOMBSTONE_RETENTION,
    claim_domain,
    delete_domain,
    find_live_by_hostname,
    get_domain,
    is_serveable,
    list_domains,
    mark_claim_verified,
    page_domains,
    purge_tombstones,
    record_check,
    request_recheck,
    transition_status,
)
from app.services.errors import DomainNotFound, InvalidStatusTransition, RateLimited


@pytest.fixture
def domain(session, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "ws_1")
    session.commit()
    return domain


def _pass_all_checks(session, domain):
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)


def _make_ready(session, domain):
    mark_claim_verified(session, domain)
    _pass_all_checks(session, domain)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    assert is_serveable(domain)


def test_suspending_the_application_stops_serving_a_ready_domain(session, domain):
    _make_ready(session, domain)
    acme = domain.application

    set_application_status(session, acme, ApplicationStatus.SUSPENDED)
    session.commit()
    assert domain.status == DomainStatus.READY
    assert not is_serveable(domain)
    assert not is_serveable(find_live_by_hostname(session, domain.hostname))

    set_application_status(session, acme, ApplicationStatus.ACTIVE)
    session.commit()
    assert is_serveable(domain)


@pytest.mark.parametrize("failing", list(CheckType))
def test_failing_check_stops_serving_and_demotes_a_ready_domain(session, domain, failing):
    _make_ready(session, domain)

    record_check(session, domain, failing, CheckStatus.FAILING, error_code="probe_failed")
    session.commit()

    assert not is_serveable(domain)
    assert domain.status == DomainStatus.ATTENTION_REQUIRED
    assert domain.events[-1].event_type == EventType.STATUS_CHANGED
    assert domain.events[-1].payload["reason"] == f"{failing.value}_check_failed"

    # Recovery requires the check to pass and an explicit return to ready.
    record_check(session, domain, failing, CheckStatus.PASSING)
    assert not is_serveable(domain)
    transition_status(session, domain, DomainStatus.READY)
    assert is_serveable(domain)


def test_failing_check_on_non_ready_domain_only_records(session, domain):
    record_check(session, domain, CheckType.OWNERSHIP, CheckStatus.FAILING, error_code="x")
    assert domain.status == DomainStatus.PENDING_DNS


def test_ready_requires_verified_claim_and_all_checks(session, domain):
    transition_status(session, domain, DomainStatus.PROVISIONING)
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, domain, DomainStatus.READY)

    mark_claim_verified(session, domain)
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, domain, DomainStatus.READY)

    for check_type in [CheckType.OWNERSHIP, CheckType.ROUTING, CheckType.CERTIFICATE]:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, domain, DomainStatus.READY)

    record_check(session, domain, CheckType.ORIGIN, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.READY, reason="probe_passed")
    session.commit()
    assert domain.status == DomainStatus.READY
    assert is_serveable(domain)


def test_invalid_transitions_are_refused(session, domain):
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, domain, DomainStatus.READY)
    transition_status(session, domain, DomainStatus.SUSPENDED)
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, domain, DomainStatus.READY)
    # Same-state transitions are a no-op, not an error.
    transition_status(session, domain, DomainStatus.SUSPENDED)


def test_record_check_updates_state_and_emits_event(session, domain):
    later = utcnow() + timedelta(minutes=5)
    check = record_check(
        session,
        domain,
        CheckType.OWNERSHIP,
        CheckStatus.FAILING,
        error_code="txt_not_found",
        message="No TXT record",
        details={"observed": []},
        next_check_at=later,
    )
    session.commit()
    assert check.status == CheckStatus.FAILING
    assert check.error_code == "txt_not_found"
    assert check.next_check_at == later
    assert check.observed_at is not None
    record_check(session, domain, CheckType.OWNERSHIP, CheckStatus.PASSING)
    assert domain.check(CheckType.OWNERSHIP).error_code is None
    events = [e for e in domain.events if e.event_type == EventType.CHECK_UPDATED]
    assert [e.payload["to"] for e in events] == ["failing", "passing"]
    assert session.scalar(select(DomainCheck).where(DomainCheck.domain_id == domain.id).limit(1))


def test_delete_leaves_tombstone_and_stops_serving(session, domain, make_application):
    acme = domain.application
    mark_claim_verified(session, domain)
    _pass_all_checks(session, domain)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    assert is_serveable(domain)

    deleted = delete_domain(session, acme, domain.id)
    session.commit()

    assert deleted.status == DomainStatus.DELETING
    assert deleted.deleted_at is not None
    assert deleted.purge_after == deleted.deleted_at + TOMBSTONE_RETENTION
    assert deleted.active_claim is None
    assert not is_serveable(deleted)
    assert find_live_by_hostname(session, "forms.customer.example") is None
    with pytest.raises(DomainNotFound):
        get_domain(session, acme, domain.id)
    assert get_domain(session, acme, domain.id, include_deleted=True).id == domain.id
    assert list_domains(session, acme) == []
    assert [d.id for d in list_domains(session, acme, include_deleted=True)] == [domain.id]
    assert deleted.events[-1].event_type == EventType.DOMAIN_DELETED


def test_delete_is_idempotent_and_terminal(session, domain):
    acme = domain.application
    first = delete_domain(session, acme, domain.id)
    stamp = first.deleted_at
    again = delete_domain(session, acme, domain.id, now=utcnow() + timedelta(days=1))
    session.commit()
    assert again.deleted_at == stamp
    with pytest.raises(InvalidStatusTransition):
        transition_status(session, again, DomainStatus.PROVISIONING)
    with pytest.raises(InvalidStatusTransition):
        mark_claim_verified(session, again)


def test_purge_removes_expired_tombstones_with_children(session, domain, make_application):
    acme = domain.application
    keep = claim_domain(session, acme, "keep.customer.example", "ws_2")
    delete_domain(session, acme, domain.id)
    delete_domain(session, acme, keep.id, now=utcnow() + timedelta(days=30))
    session.commit()

    purged = purge_tombstones(session, now=utcnow() + TOMBSTONE_RETENTION + timedelta(seconds=1))
    session.commit()
    session.expire_all()

    assert purged == 1
    assert session.get(Domain, domain.id) is None
    assert session.get(Domain, keep.id) is not None
    assert (
        session.scalar(select(OwnershipClaim).where(OwnershipClaim.domain_id == domain.id)) is None
    )
    assert session.scalar(select(DomainCheck).where(DomainCheck.domain_id == domain.id)) is None
    assert session.scalar(select(DomainEvent).where(DomainEvent.domain_id == domain.id)) is None


def test_check_constraints_reject_unknown_status(session, domain):
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE domains SET status = 'bogus' WHERE hostname = :hostname"),
            {"hostname": domain.hostname},
        )
    session.rollback()


def test_only_one_live_claim_per_domain(session, domain):
    session.add(
        OwnershipClaim(
            domain_id=domain.id,
            txt_record_name="x",
            token="t2",
            cname_target="acme.edge.example.net",
            status=ClaimStatus.PENDING,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_recheck_interval_per_domain_and_budget_per_application(
    session, domain, monkeypatch, make_application
):
    from app.services import domains as domain_service

    acme = domain.application
    t0 = utcnow()
    request_recheck(session, acme, domain.id, now=t0)
    with pytest.raises(RateLimited) as info:
        request_recheck(session, acme, domain.id, now=t0 + timedelta(seconds=30))
    assert 29 <= info.value.retry_after <= 31
    request_recheck(session, acme, domain.id, now=t0 + timedelta(seconds=61))

    monkeypatch.setattr(domain_service, "RECHECK_MAX_PER_WINDOW", 3)
    other = claim_domain(session, acme, "other.customer.example", "ws_2")
    request_recheck(session, acme, other.id, now=t0 + timedelta(seconds=62))
    third = claim_domain(session, acme, "third.customer.example", "ws_3")
    with pytest.raises(RateLimited) as info:
        request_recheck(session, acme, third.id, now=t0 + timedelta(seconds=63))
    assert info.value.retry_after >= 3500
    # The window slides: an hour after the first recheck the budget frees up.
    request_recheck(session, acme, third.id, now=t0 + timedelta(hours=1, seconds=2))


def test_recheck_refuses_deleted_domains(session, domain):
    acme = domain.application
    delete_domain(session, acme, domain.id)
    with pytest.raises(InvalidStatusTransition):
        request_recheck(session, acme, domain.id)


def test_page_domains_lookahead_is_not_clamped(session, domain):
    from app.services.domains import MAX_PAGE_SIZE

    acme = domain.application
    for index in range(MAX_PAGE_SIZE):
        claim_domain(session, acme, f"p{index}.customer.example", "w")
    session.commit()
    rows, has_more = page_domains(session, acme, limit=MAX_PAGE_SIZE)
    assert len(rows) == MAX_PAGE_SIZE and has_more
    rows, has_more = page_domains(session, acme, limit=MAX_PAGE_SIZE, offset=MAX_PAGE_SIZE)
    assert len(rows) == 1 and not has_more
    rows, has_more = page_domains(session, acme, limit=MAX_PAGE_SIZE * 5)
    assert len(rows) == MAX_PAGE_SIZE and has_more


def _concurrent_rechecks(session_factory, application_id, domain_ids, workers):
    import threading

    from app.models import Application

    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        with session_factory() as s:
            app_row = s.get(Application, application_id)
            s.commit()  # end the read transaction; the recheck must start fresh
            barrier.wait()
            try:
                request_recheck(s, app_row, domain_ids[index % len(domain_ids)])
                s.commit()
                result = "accepted"
            except RateLimited:
                s.rollback()
                result = "limited"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(outcomes) == workers
    return outcomes


def test_concurrent_rechecks_on_one_domain_admit_exactly_one(session_factory, session, domain):
    outcomes = _concurrent_rechecks(session_factory, domain.application_id, [domain.id], 6)
    assert outcomes.count("accepted") == 1
    assert outcomes.count("limited") == 5
    with session_factory() as s:
        events = s.scalars(
            select(DomainEvent).where(
                DomainEvent.domain_id == domain.id,
                DomainEvent.event_type == EventType.RECHECK_REQUESTED.value,
            )
        ).all()
        assert len(events) == 1


def test_concurrent_rechecks_respect_the_application_budget(
    session_factory, session, domain, monkeypatch
):
    from app.services import domains as domain_service

    monkeypatch.setattr(domain_service, "RECHECK_MAX_PER_WINDOW", 2)
    acme = domain.application
    ids = [domain.id]
    for index in range(5):
        ids.append(claim_domain(session, acme, f"c{index}.customer.example", "w").id)
    session.commit()

    outcomes = _concurrent_rechecks(session_factory, acme.id, ids, 6)
    assert outcomes.count("accepted") == 2
    assert outcomes.count("limited") == 4


def test_events_recorded_with_one_clock_keep_a_total_order(session, domain):
    from app.services.domains import record_event

    stamp = utcnow()
    first = record_event(session, domain, "test.one", now=stamp)
    second = record_event(session, domain, "test.two", now=stamp)
    third = record_event(session, domain, "test.three", now=stamp - timedelta(days=1))
    session.commit()
    session.refresh(domain)
    assert first.created_at < second.created_at < third.created_at
    assert [e.event_type for e in domain.events][-3:] == ["test.one", "test.two", "test.three"]
