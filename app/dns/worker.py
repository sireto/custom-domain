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
) -> bool:
    """Claim the domain's due checks for this run; False if another worker got there first."""
    result = session.execute(
        update(DomainCheck)
        .where(
            DomainCheck.domain_id == domain_id, DomainCheck.check_type.in_(tuple(types)), _due(now)
        )
        .values(next_check_at=now + LEASE)
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
) -> bool:
    """Run the domain's due checks. Returns False if it was skipped."""
    now = now or utcnow()
    settings = settings or EdgeSettings()
    prober = prober or SystemEdgeProber(settings)
    with session_factory() as session:
        types = due_check_types(session, domain_id, now)
        session.rollback()
        if not types:
            return False
        if not lease_domain(session, domain_id, now, types):
            session.rollback()
            return False
        session.commit()
        domain = _load(session, domain_id)
        if domain is None:
            session.rollback()
            return False
        application_id = domain.application_id
        session.commit()

        dns_due = bool(types & set(DNS_CHECK_TYPES))
        edge_due = bool(types & set(EDGE_CHECK_TYPES))
        if dns_due:
            ownership = ownership_outcome(resolver, domain)
            routing = routing_outcome(resolver, domain)
            lock_application(session, application_id)
            domain = _load(session, domain_id)
            if domain is None:
                session.rollback()
                return False  # deleted while we were querying
            apply_dns_outcomes(session, domain, ownership, routing, now=utcnow())
            session.commit()

        if not (dns_due or edge_due):
            return True
        # Re-read after the DNS phase: a just-verified claim makes the domain eligible.
        domain = _load(session, domain_id)
        if domain is None:
            session.rollback()
            return True
        eligible = eligible_for_edge_checks(domain)
        session.commit()
        if not eligible:
            return True
        certificate = certificate_outcome(prober, domain, settings, now=utcnow())
        lock_application(session, application_id)
        domain = _load(session, domain_id)
        if domain is None or not eligible_for_edge_checks(domain):
            session.rollback()
            return True
        apply_edge_outcomes(session, domain, certificate, origin_outcome(domain), now=utcnow())
        session.commit()
        return True


def run_due_checks(
    session_factory: Callable[[], Session],
    resolver: Resolver,
    *,
    prober: EdgeProber | None = None,
    settings: EdgeSettings | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> WorkerResult:
    now = now or utcnow()
    with session_factory() as session:
        ids = due_domain_ids(session, now, limit)
    processed = failed = 0
    for domain_id in ids:
        try:
            if process_domain(
                session_factory, resolver, domain_id, prober=prober, settings=settings, now=now
            ):
                processed += 1
        except Exception:  # one bad domain must not stop the batch
            failed += 1
            logger.exception("checks failed for domain %s", domain_id)
    return WorkerResult(now, processed, failed)


class ChecksWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        resolver: Resolver,
        *,
        prober: EdgeProber | None = None,
        settings: EdgeSettings | None = None,
        batch_size: int = 50,
    ) -> None:
        self.session_factory = session_factory
        self.resolver = resolver
        self.settings = settings or EdgeSettings()
        self.prober = prober or SystemEdgeProber(self.settings)
        self.batch_size = batch_size
        self.last_result: WorkerResult | None = None

    def run_once(self) -> WorkerResult:
        try:
            result = run_due_checks(
                self.session_factory,
                self.resolver,
                prober=self.prober,
                settings=self.settings,
                limit=self.batch_size,
            )
        except Exception as exc:  # database unavailable: report and retry next tick
            logger.error("checks worker run failed: %s", exc)
            result = WorkerResult(utcnow(), 0, 0)
        self.last_result = result
        return result

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while True:
            self.run_once()
            if stop.wait(interval):
                return


DnsWorker = ChecksWorker
