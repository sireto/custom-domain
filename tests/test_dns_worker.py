import threading
from datetime import timedelta

from app.dns.worker import (
    LEASE,
    ChecksWorker,
    due_domain_ids,
    lease_domain,
    process_domain,
    run_due_checks,
)
from app.edge.probe import EdgeProbeFailed
from app.edge.settings import EdgeSettings
from app.models import CheckType, DomainStatus
from app.models.types import utcnow
from app.services.domains import claim_domain, delete_domain, request_recheck
from tests.test_dns_checks import TARGET, FakeResolver


def _refresh(session):
    # End the test session's read transaction so it sees the worker's commits
    # (SQLite keeps a snapshot for the life of a transaction).
    session.commit()
    session.expire_all()


class NoEdge:
    """Prober that never reaches an edge; keeps worker tests off the network."""

    def probe(self, hostname):
        raise EdgeProbeFailed("connection_failed", "no edge in tests")


SETTINGS = EdgeSettings(reconcile_enabled=True, legacy_api_enabled=False)
KW = {"prober": NoEdge(), "settings": SETTINGS}


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

    result = run_due_checks(session_factory, resolver, **KW)
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
    assert run_due_checks(session_factory, resolver, **KW).processed == 0
    request_recheck(session, acme, bad.id)
    session.commit()
    _publish(resolver, bad)
    assert run_due_checks(session_factory, resolver, **KW).processed == 1
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
    assert process_domain(session_factory, resolver, domain.id, **KW) is False
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
        totals.append(run_due_checks(session_factory, resolver, **KW).processed)

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
    worker = ChecksWorker(session_factory, resolver, **KW)
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


def test_worker_runs_edge_checks_after_dns_and_reaches_ready(
    session, session_factory, make_application
):
    from app.models import CheckType
    from app.services.applications import (
        activate_origin,
        record_origin_verification,
        register_origin,
    )
    from tests.test_edge_checks import FakeProber

    acme = make_application("acme")
    origin = register_origin(session, acme, host="app.acme.example")
    record_origin_verification(session, origin, verified=True)
    activate_origin(session, origin)
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    resolver = FakeResolver()
    _publish(resolver, domain)
    prober = FakeProber()

    result = run_due_checks(session_factory, resolver, prober=prober, settings=SETTINGS)
    assert result.processed == 1 and result.failed == 0
    _refresh(session)
    assert domain.status == DomainStatus.READY
    assert prober.calls == ["forms.customer.example"]
    assert domain.check(CheckType.CERTIFICATE).status.value == "passing"
    assert domain.check(CheckType.ORIGIN).status.value == "passing"

    # Nothing due afterwards; the probe is not repeated until revalidation.
    assert (
        run_due_checks(session_factory, resolver, prober=prober, settings=SETTINGS).processed == 0
    )
    assert prober.calls == ["forms.customer.example"]


def test_worker_skips_edge_checks_for_unverified_domains(
    session, session_factory, make_application
):
    from tests.test_edge_checks import FakeProber

    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    prober = FakeProber()
    result = run_due_checks(session_factory, FakeResolver(), prober=prober, settings=SETTINGS)
    assert result.processed == 1
    _refresh(session)
    assert domain.status == DomainStatus.PENDING_DNS and prober.calls == []


def test_claim_reissued_during_queries_discards_the_results(
    session, session_factory, make_application
):
    from app.models import Application, ClaimStatus
    from app.services.domains import reissue_claim

    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    old_claim_id = domain.active_claim.id

    class ReissuingResolver(FakeResolver):
        """Re-issues the claim while the worker is inside its DNS queries."""

        def txt(self, name):
            with session_factory() as s:
                app_row = s.get(Application, acme.id)
                s.commit()
                reissue_claim(s, app_row, domain.id)
                s.commit()
            return super().txt(name)

    resolver = ReissuingResolver()
    _publish(resolver, domain)  # records for the OLD token and target
    assert process_domain(session_factory, resolver, domain.id, **KW) is False

    _refresh(session)
    fresh = domain.active_claim
    assert fresh.id != old_claim_id and fresh.status == ClaimStatus.PENDING
    assert domain.status == DomainStatus.PENDING_DNS
    for check_type in (CheckType.OWNERSHIP, CheckType.ROUTING):
        check = domain.check(check_type)
        # Reset by the re-issue, not evaluated by the worker's stale results.
        assert check.status.value == "pending"
        assert check.details.get("reason") == "claim_reissued"
        assert check.next_check_at is not None and check.next_check_at <= utcnow()

    # The next run verifies the new claim on its own records only.
    plain = FakeResolver()
    _publish(plain, domain)
    assert process_domain(session_factory, plain, domain.id, **KW) is True
    _refresh(session)
    assert domain.active_claim.status == ClaimStatus.VERIFIED
    assert domain.status == DomainStatus.PROVISIONING
