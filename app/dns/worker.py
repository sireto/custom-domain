"""Scheduler that runs due lifecycle checks (DNS, certificate, origin).

Work is claimed per domain with a short lease on the due check rows, so
several API instances can run the worker without checking the same domain
twice. Network work (DNS queries, the readiness probe) happens outside any
database transaction; results are applied under the application lock in
short transactions.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.dns.resolver import Resolver
from app.edge.settings import EdgeSettings
from app.models import CheckType, Domain, DomainCheck, DomainStatus
from app.models.types import utcnow
from app.services.dns_checks import apply_dns_outcomes, ownership_outcome, routing_outcome
from app.services.domains import _domain_query, lock_application
from app.services.edge_checks import (
    EDGE_CHECK_TYPES,
    EdgeProber,
    SystemEdgeProber,
    apply_edge_outcomes,
    certificate_outcome,
    eligible_for_edge_checks,
    origin_outcome,
)

logger = logging.getLogger(__name__)

DNS_CHECK_TYPES = (CheckType.OWNERSHIP, CheckType.ROUTING)
LEASE = timedelta(minutes=2)


@dataclass(frozen=True)
class WorkerResult:
    at: datetime
    processed: int
    failed: int
    status_changes: int = 0


@dataclass(frozen=True)
class Processed:
    done: bool
    status_changed: bool = False


def _due(now: datetime):
    return or_(DomainCheck.next_check_at.is_(None), DomainCheck.next_check_at <= now)


def due_domain_ids(session: Session, now: datetime, limit: int) -> list[uuid.UUID]:
    rows = session.execute(
        select(DomainCheck.domain_id)
        .join(Domain, Domain.id == DomainCheck.domain_id)
        .where(_due(now), Domain.deleted_at.is_(None), Domain.status != DomainStatus.DELETING)
        .group_by(DomainCheck.domain_id)
        .order_by(DomainCheck.domain_id)
        .limit(limit)
    ).all()
    return [row[0] for row in rows]


def due_check_types(session: Session, domain_id: uuid.UUID, now: datetime) -> set[CheckType]:
    rows = session.execute(
        select(DomainCheck.check_type).where(DomainCheck.domain_id == domain_id, _due(now))
    ).all()
    return {row[0] for row in rows}


def lease_domain(
    session: Session,
    domain_id: uuid.UUID,
    now: datetime,
    types: Iterable[CheckType] = DNS_CHECK_TYPES,
    *,
    force: bool = False,
) -> bool:
    """Claim the domain's due checks for this run; False if another worker got there first.

    With ``force`` the checks are claimed whether or not they are due, for
    checks that must follow a status change immediately (a freshly verified
    domain's certificate and origin checks).
    """
    conditions = [DomainCheck.domain_id == domain_id, DomainCheck.check_type.in_(tuple(types))]
    if not force:
        conditions.append(_due(now))
    result = session.execute(
        update(DomainCheck).where(*conditions).values(next_check_at=now + LEASE)
    )
    return bool(result.rowcount)


def _load(session: Session, domain_id: uuid.UUID) -> Domain | None:
    domain = session.scalar(_domain_query(include_deleted=False).where(Domain.id == domain_id))
    if domain is None or domain.status == DomainStatus.DELETING:
        return None
    return domain


def process_domain(
    session_factory: Callable[[], Session],
    resolver: Resolver,
    domain_id: uuid.UUID,
    *,
    prober: EdgeProber | None = None,
    settings: EdgeSettings | None = None,
    now: datetime | None = None,
    on_status_change: Callable[[], object] | None = None,
) -> Processed:
    """Run the domain's due checks. ``done`` is False if it was skipped.

    ``on_status_change`` (the edge reconciler) is called between the DNS phase
    and the edge phase when the DNS phase changed the domain's status, so a
    freshly verified domain is routed before its readiness probe runs.

    ``now`` defaults to the current time for this domain, not the batch's
    start, so a lease taken late in a slow batch still lasts ``LEASE``.
    """
    now = now or utcnow()
    settings = settings or EdgeSettings()
    prober = prober or SystemEdgeProber(settings)
    with session_factory() as session:
        types = due_check_types(session, domain_id, now)
        session.rollback()
        if not types:
            return Processed(False)
        if not lease_domain(session, domain_id, now, types):
            session.rollback()
            return Processed(False)
        session.commit()
        domain = _load(session, domain_id)
        if domain is None:
            session.rollback()
            return Processed(False)
        application_id = domain.application_id
        status_before = domain.status
        session.commit()

        dns_due = bool(types & set(DNS_CHECK_TYPES))
        edge_due = bool(types & set(EDGE_CHECK_TYPES))
        if dns_due:
            queried = claim_version(domain)
            ownership = ownership_outcome(resolver, domain)
            routing = routing_outcome(resolver, domain)
            lock_application(session, application_id)
            # Reload from the database, not from the identity map, so a claim
            # re-issued during the queries is visible here.
            session.expire_all()
            domain = _load(session, domain_id)
            if domain is None:
                session.rollback()
                return Processed(False)  # deleted while we were querying
            if claim_version(domain) != queried:
                # The observations belong to the old token and target: discard
                # them and make the DNS checks due again for the new claim.
                logger.info("discarding dns results for %s: claim changed", domain.hostname)
                session.execute(
                    update(DomainCheck)
                    .where(
                        DomainCheck.domain_id == domain_id,
                        DomainCheck.check_type.in_(DNS_CHECK_TYPES),
                    )
                    .values(next_check_at=utcnow())
                )
                session.commit()
                return Processed(False)
            apply_dns_outcomes(session, domain, ownership, routing, now=utcnow())
            dns_changed = domain.status != status_before
            if dns_changed and not edge_due:
                # A status change (a fresh verification) makes the certificate
                # and origin checks follow right away: claim them for this run.
                edge_due = lease_domain(session, domain_id, utcnow(), EDGE_CHECK_TYPES, force=True)
            session.commit()
            if dns_changed and on_status_change is not None:
                try:
                    on_status_change()
                except Exception as exc:  # the hook must not break the checks
                    logger.error("status change hook failed: %s", exc)

        # Re-read after the DNS phase: a just-verified claim makes the domain eligible.
        domain = _load(session, domain_id)
        if domain is None:
            session.rollback()
            return Processed(True, domain_changed(status_before, None))
        if not edge_due or not eligible_for_edge_checks(domain):
            # Only leased checks run here: another worker may hold the edge
            # checks, or they are simply not due yet.
            changed = domain.status != status_before
            session.commit()
            return Processed(True, changed)
        # Load what the probes need before leaving the transaction.
        _ = domain.application.serving_origin
        probed = claim_version(domain)
        session.commit()
        certificate = certificate_outcome(prober, domain, settings, now=utcnow())
        origin = origin_outcome(domain, prober, settings)
        lock_application(session, application_id)
        # As in the DNS phase: reload from the database so a claim re-issued
        # or a domain deleted during the probes is seen before anything is
        # recorded against it.
        session.expire_all()
        domain = _load(session, domain_id)
        if domain is None:
            session.rollback()
            return Processed(True, domain_changed(status_before, None))
        if claim_version(domain) != probed or not eligible_for_edge_checks(domain):
            logger.info("discarding edge results for %s: claim changed", domain.hostname)
            changed = domain.status != status_before
            session.rollback()
            return Processed(True, changed)
        apply_edge_outcomes(session, domain, certificate, origin, now=utcnow())
        changed = domain.status != status_before
        session.commit()
        return Processed(True, changed)


def claim_version(domain: Domain) -> tuple | None:
    """Identity of the live claim the DNS observations were made against."""
    claim = domain.active_claim
    if claim is None:
        return None
    return (claim.id, claim.token, claim.cname_target)


def domain_changed(before, after) -> bool:
    return before != after


def run_due_checks(
    session_factory: Callable[[], Session],
    resolver: Resolver,
    *,
    prober: EdgeProber | None = None,
    settings: EdgeSettings | None = None,
    now: datetime | None = None,
    limit: int = 50,
    on_status_change: Callable[[], object] | None = None,
) -> WorkerResult:
    # A fixed ``now`` (tests) is passed through; otherwise each domain takes
    # its own current time so leases and timestamps do not age with the batch.
    batch_now = now or utcnow()
    with session_factory() as session:
        ids = due_domain_ids(session, batch_now, limit)
    processed = failed = changes = 0
    for domain_id in ids:
        try:
            outcome = process_domain(
                session_factory,
                resolver,
                domain_id,
                prober=prober,
                settings=settings,
                now=now,
                on_status_change=on_status_change,
            )
            if outcome.done:
                processed += 1
            if outcome.status_changed:
                changes += 1
        except Exception:  # one bad domain must not stop the batch
            failed += 1
            logger.exception("checks failed for domain %s", domain_id)
    return WorkerResult(batch_now, processed, failed, changes)


class ChecksWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        resolver: Resolver,
        *,
        prober: EdgeProber | None = None,
        settings: EdgeSettings | None = None,
        batch_size: int = 50,
        on_status_change: Callable[[], object] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.resolver = resolver
        self.settings = settings or EdgeSettings()
        self.prober = prober or SystemEdgeProber(self.settings)
        self.batch_size = batch_size
        # Called after a batch in which a domain changed status, so the edge
        # configuration follows without waiting for the reconciler's timer.
        self.on_status_change = on_status_change
        self.last_result: WorkerResult | None = None

    def run_once(self) -> WorkerResult:
        try:
            result = run_due_checks(
                self.session_factory,
                self.resolver,
                prober=self.prober,
                settings=self.settings,
                limit=self.batch_size,
                on_status_change=self.on_status_change,
            )
        except Exception as exc:  # database unavailable: report and retry next tick
            logger.error("checks worker run failed: %s", exc)
            result = WorkerResult(utcnow(), 0, 0)
        self.last_result = result
        if result.status_changes and self.on_status_change is not None:
            try:
                self.on_status_change()
            except Exception as exc:  # the hook must not break the loop
                logger.error("status change hook failed: %s", exc)
        return result

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while True:
            self.run_once()
            if stop.wait(interval):
                return


DnsWorker = ChecksWorker
