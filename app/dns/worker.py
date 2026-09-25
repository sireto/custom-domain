"""Scheduler that runs due DNS checks.

Work is claimed per domain with a short lease on the check rows, so several
API instances can run the worker without checking the same domain twice.
DNS queries happen outside any database transaction; results are applied in
a second, short transaction.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.dns.resolver import Resolver
from app.models import CheckType, Domain, DomainCheck, DomainStatus
from app.models.types import utcnow
from app.services.dns_checks import apply_dns_outcomes, ownership_outcome, routing_outcome
from app.services.domains import _domain_query, lock_application

logger = logging.getLogger(__name__)

DNS_CHECK_TYPES = (CheckType.OWNERSHIP, CheckType.ROUTING)
LEASE = timedelta(minutes=2)


@dataclass(frozen=True)
class WorkerResult:
    at: datetime
    processed: int
    failed: int


def due_domain_ids(session: Session, now: datetime, limit: int) -> list[uuid.UUID]:
    due = or_(DomainCheck.next_check_at.is_(None), DomainCheck.next_check_at <= now)
    rows = session.execute(
        select(DomainCheck.domain_id)
        .join(Domain, Domain.id == DomainCheck.domain_id)
        .where(
            DomainCheck.check_type.in_(DNS_CHECK_TYPES),
            due,
            Domain.deleted_at.is_(None),
            Domain.status != DomainStatus.DELETING,
        )
        .group_by(DomainCheck.domain_id)
        .order_by(DomainCheck.domain_id)
        .limit(limit)
    ).all()
    return [row[0] for row in rows]


def lease_domain(session: Session, domain_id: uuid.UUID, now: datetime) -> bool:
    """Claim the domain's DNS checks for this run; False if another worker got there first."""
    due = or_(DomainCheck.next_check_at.is_(None), DomainCheck.next_check_at <= now)
    result = session.execute(
        update(DomainCheck)
        .where(DomainCheck.domain_id == domain_id, DomainCheck.check_type.in_(DNS_CHECK_TYPES), due)
        .values(next_check_at=now + LEASE)
    )
    return bool(result.rowcount)


def process_domain(
    session_factory: Callable[[], Session],
    resolver: Resolver,
    domain_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Run both DNS checks for one domain. Returns False if it was skipped."""
    now = now or utcnow()
    with session_factory() as session:
        if not lease_domain(session, domain_id, now):
            session.rollback()
            return False
        session.commit()
        domain = session.scalar(_domain_query(include_deleted=False).where(Domain.id == domain_id))
        if domain is None or domain.status == DomainStatus.DELETING:
            return False
        # Queries run without holding a transaction; they can be slow.
        session.commit()
        application_id = domain.application_id
        ownership = ownership_outcome(resolver, domain)
        routing = routing_outcome(resolver, domain)
        # Write phase: take the application lock first so the read that
        # follows is not a stale snapshot (matters on SQLite) and so results
        # for one application are applied one at a time.
        lock_application(session, application_id)
        domain = session.scalar(_domain_query(include_deleted=False).where(Domain.id == domain_id))
        if domain is None or domain.status == DomainStatus.DELETING:
            session.rollback()
            return False  # deleted while we were querying
        apply_dns_outcomes(session, domain, ownership, routing, now=utcnow())
        session.commit()
        return True


def run_due_checks(
    session_factory: Callable[[], Session],
    resolver: Resolver,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> WorkerResult:
    now = now or utcnow()
    with session_factory() as session:
        ids = due_domain_ids(session, now, limit)
    processed = failed = 0
    for domain_id in ids:
        try:
            if process_domain(session_factory, resolver, domain_id, now=now):
                processed += 1
        except Exception:  # one bad domain must not stop the batch
            failed += 1
            logger.exception("dns checks failed for domain %s", domain_id)
    return WorkerResult(now, processed, failed)


class DnsWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        resolver: Resolver,
        *,
        batch_size: int = 50,
    ) -> None:
        self.session_factory = session_factory
        self.resolver = resolver
        self.batch_size = batch_size
        self.last_result: WorkerResult | None = None

    def run_once(self) -> WorkerResult:
        try:
            result = run_due_checks(self.session_factory, self.resolver, limit=self.batch_size)
        except Exception as exc:  # database unavailable: report and retry next tick
            logger.error("dns worker run failed: %s", exc)
            result = WorkerResult(utcnow(), 0, 0)
        self.last_result = result
        return result

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while True:
            self.run_once()
            if stop.wait(interval):
                return
