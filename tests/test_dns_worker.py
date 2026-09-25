import threading
from datetime import timedelta

from app.dns.worker import (
    LEASE,
    DnsWorker,
    due_domain_ids,
    lease_domain,
    process_domain,
    run_due_checks,
)
from app.models import CheckType, DomainStatus
from app.models.types import utcnow
from app.services.domains import claim_domain, delete_domain, request_recheck
from tests.test_dns_checks import TARGET, FakeResolver


def _refresh(session):
    # End the test session's read transaction so it sees the worker's commits
    # (SQLite keeps a snapshot for the life of a transaction).
    session.commit()
    session.expire_all()


def _publish(resolver, domain):
    claim = domain.active_claim
    resolver.txt_records[claim.txt_record_name] = [claim.txt_record_value]
    resolver.cnames[domain.hostname] = TARGET


def test_due_selection_excludes_deleted_and_scheduled(session, make_application):
    acme = make_application("acme")
    now = utcnow()
    fresh = claim_domain(session, acme, "fresh.customer.example", "w")
    later = claim_domain(session, acme, "later.customer.example", "w")
    gone = claim_domain(session, acme, "gone.customer.example", "w")
    for check in later.checks:
        check.next_check_at = now + timedelta(hours=1)
    delete_domain(session, acme, gone.id)
    session.commit()
    assert due_domain_ids(session, now, 50) == [fresh.id]
    assert due_domain_ids(session, now + timedelta(hours=2), 50) == sorted([fresh.id, later.id])


def test_lease_prevents_double_processing(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    now = utcnow()
    with session_factory() as first, session_factory() as second:
        assert lease_domain(first, domain.id, now)
        first.commit()
        assert not lease_domain(second, domain.id, now)
        second.rollback()
    _refresh(session)
    assert all(
        c.next_check_at == now + LEASE
        for c in domain.checks
        if c.check_type in (CheckType.OWNERSHIP, CheckType.ROUTING)
    )
    assert domain.check(CheckType.CERTIFICATE).next_check_at is None


def test_run_due_checks_processes_and_reschedules(session, session_factory, make_application):
    acme = make_application("acme")
    resolver = FakeResolver()
    ok = claim_domain(session, acme, "ok.customer.example", "w")
    bad = claim_domain(session, acme, "bad.customer.example", "w")
    session.commit()
    _publish(resolver, ok)

    result = run_due_checks(session_factory, resolver)
    assert result.processed == 2 and result.failed == 0
    _refresh(session)
    assert ok.status == DomainStatus.PROVISIONING
    assert bad.status == DomainStatus.PENDING_DNS
    assert bad.check(CheckType.OWNERSHIP).error_code == "txt_record_not_found"
    assert all(
        c.next_check_at > utcnow()
        for d in (ok, bad)
        for c in d.checks
        if c.check_type != CheckType.CERTIFICATE and c.check_type != CheckType.ORIGIN
    )

    # Nothing is due until the backoff elapses; a manual recheck brings it forward.
    assert run_due_checks(session_factory, resolver).processed == 0
    request_recheck(session, acme, bad.id)
    session.commit()
    _publish(resolver, bad)
    assert run_due_checks(session_factory, resolver).processed == 1
    _refresh(session)
    assert bad.status == DomainStatus.PROVISIONING


def test_domain_deleted_during_queries_is_not_updated(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()

    class DeletingResolver(FakeResolver):
        def txt(self, name):
            with session_factory() as s:
                from app.models import Application

                app_row = s.get(Application, acme.id)
                s.commit()
                delete_domain(s, app_row, domain.id)
                s.commit()
            return super().txt(name)

    resolver = DeletingResolver()
    _publish(resolver, domain)
    assert process_domain(session_factory, resolver, domain.id) is False
    _refresh(session)
    assert domain.deleted_at is not None and domain.status == DomainStatus.DELETING


def test_concurrent_workers_share_the_batch_without_duplicates(
    session, session_factory, make_application
):
    acme = make_application("acme")
    resolver = FakeResolver()
    domains = [claim_domain(session, acme, f"d{i}.customer.example", "w") for i in range(6)]
    session.commit()
    for d in domains:
        _publish(resolver, d)
    barrier = threading.Barrier(3)
    totals = []

    def worker():
        barrier.wait()
        totals.append(run_due_checks(session_factory, resolver).processed)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sum(totals) == 6
    _refresh(session)
    assert all(d.status == DomainStatus.PROVISIONING for d in domains)


def test_worker_loop_runs_and_stops(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    resolver = FakeResolver()
    _publish(resolver, domain)
    worker = DnsWorker(session_factory, resolver)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop, 60))
    thread.start()
    for _ in range(200):
        if worker.last_result is not None:
            break
        threading.Event().wait(0.05)
    stop.set()
    thread.join(5)
    assert worker.last_result is not None and worker.last_result.processed == 1
