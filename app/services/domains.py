"""Domain claims, lifecycle state and tombstones.

Rules enforced here (see docs/data-model.md):

* A hostname has at most one live domain row across all applications. The
  database enforces it with a partial unique index, so concurrent creates
  cannot both succeed.
* Every read and write takes the calling application; a domain that belongs
  to another application is reported as not found.
* Deletion is a tombstone. The row, its claim and its history stay for a
  retention period, the claim is revoked, and nothing marks the row as
  serveable again. Re-claiming the hostname creates a new row with a new token.
* ``ready`` can only be entered when every check passes and the live claim is
  verified. Readiness is never inferred from DNS alone.
* The edge authorization rule (``is_serveable``) re-evaluates the application
  status, the claim and every check on each call, so a suspended tenant or a
  failing check stops service without waiting for a status transition.
* Re-issuing ownership material moves the domain back to ``pending_dns`` and
  resets every check; the new token must be verified and all checks must pass
  again before the domain can serve.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.hostname import canonicalize
from app.models import (
    Application,
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
from app.services.errors import (
    ApplicationSuspended,
    DomainNotFound,
    HostnameAlreadyClaimed,
    InvalidReference,
    InvalidStatusTransition,
)

CHALLENGE_LABEL = "_custom-domain-challenge"
CLAIM_TOKEN_BYTES = 32
TOMBSTONE_RETENTION = timedelta(days=90)
MAX_REFERENCE_LENGTH = 255
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

ALLOWED_TRANSITIONS: dict[DomainStatus, frozenset[DomainStatus]] = {
    DomainStatus.PENDING_DNS: frozenset(
        {
            DomainStatus.PROVISIONING,
            DomainStatus.ATTENTION_REQUIRED,
            DomainStatus.SUSPENDED,
            DomainStatus.DELETING,
        }
    ),
    DomainStatus.PROVISIONING: frozenset(
        {
            DomainStatus.READY,
            DomainStatus.PENDING_DNS,
            DomainStatus.ATTENTION_REQUIRED,
            DomainStatus.SUSPENDED,
            DomainStatus.DELETING,
        }
    ),
    DomainStatus.READY: frozenset(
        {
            DomainStatus.PENDING_DNS,
            DomainStatus.ATTENTION_REQUIRED,
            DomainStatus.SUSPENDED,
            DomainStatus.DELETING,
        }
    ),
    DomainStatus.ATTENTION_REQUIRED: frozenset(
        {
            DomainStatus.PENDING_DNS,
            DomainStatus.PROVISIONING,
            DomainStatus.READY,
            DomainStatus.SUSPENDED,
            DomainStatus.DELETING,
        }
    ),
    DomainStatus.SUSPENDED: frozenset(
        {DomainStatus.PENDING_DNS, DomainStatus.PROVISIONING, DomainStatus.DELETING}
    ),
    DomainStatus.DELETING: frozenset(),
}

# Statuses that fall back to pending_dns when ownership material is re-issued.
RESET_ON_REISSUE = frozenset(
    {DomainStatus.PROVISIONING, DomainStatus.READY, DomainStatus.ATTENTION_REQUIRED}
)


# --- claims -----------------------------------------------------------------


def claim_domain(
    session: Session,
    application: Application,
    hostname: str,
    reference: str,
    *,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Domain:
    """Atomically claim ``hostname`` for ``application`` and issue ownership material.

    Raises ``InvalidHostname`` for unusable names and ``HostnameAlreadyClaimed``
    when a live row exists in any application. The loser of a concurrent claim
    gets the same error; the session stays usable because the insert runs in a
    savepoint.
    """
    if application.status != ApplicationStatus.ACTIVE:
        raise ApplicationSuspended()
    if not reference or not reference.strip() or len(reference) > MAX_REFERENCE_LENGTH:
        raise InvalidReference(f"Reference must be 1-{MAX_REFERENCE_LENGTH} characters")
    canonical = canonicalize(hostname)
    now = now or utcnow()

    domain = Domain(
        application_id=application.id,
        hostname=canonical,
        reference=reference.strip(),
        status=DomainStatus.PENDING_DNS,
        extra=metadata,
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(domain)
            session.flush()
    except IntegrityError as exc:
        if "hostname" in str(exc.orig).lower():
            raise HostnameAlreadyClaimed(f"{canonical} is already claimed") from exc
        raise

    for check_type in CheckType:
        session.add(
            DomainCheck(domain_id=domain.id, check_type=check_type, status=CheckStatus.PENDING)
        )
    record_event(
        session,
        domain,
        EventType.DOMAIN_CREATED,
        {"hostname": canonical, "reference": domain.reference},
        now=now,
    )
    _issue_claim(session, domain, application.cname_target, now=now)
    session.flush()
    session.refresh(domain)
    return domain


def _issue_claim(
    session: Session, domain: Domain, cname_target: str, *, now: datetime
) -> OwnershipClaim:
    claim = OwnershipClaim(
        domain_id=domain.id,
        txt_record_name=f"{CHALLENGE_LABEL}.{domain.hostname}",
        token=secrets.token_urlsafe(CLAIM_TOKEN_BYTES),
        cname_target=cname_target,
        status=ClaimStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    session.add(claim)
    session.flush()
    record_event(
        session,
        domain,
        EventType.CLAIM_ISSUED,
        {"claim_id": str(claim.id), "txt_record_name": claim.txt_record_name},
        now=now,
    )
    return claim


def reissue_claim(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> OwnershipClaim:
    """Revoke the live claim and issue fresh material. Old tokens never verify again.

    The domain leaves ``ready`` (or ``provisioning`` / ``attention_required``)
    for ``pending_dns`` and every check is reset to pending, so nothing that
    was established under the old claim carries over. A ``suspended`` domain
    stays suspended; only policy lifts a suspension.
    """
    now = now or utcnow()
    domain = get_domain(session, application, domain_id)
    _revoke_live_claim(session, domain, reason="reissued", now=now)
    for check_type in CheckType:
        record_check(
            session,
            domain,
            check_type,
            CheckStatus.PENDING,
            details={"reason": "claim_reissued"},
            observed_at=now,
        )
    if domain.status in RESET_ON_REISSUE:
        transition_status(
            session, domain, DomainStatus.PENDING_DNS, reason="claim_reissued", now=now
        )
    claim = _issue_claim(session, domain, application.cname_target, now=now)
    session.refresh(domain)
    return claim


def mark_claim_verified(
    session: Session,
    domain: Domain,
    *,
    method: str = "dns_txt",
    now: datetime | None = None,
) -> OwnershipClaim:
    now = now or utcnow()
    claim = domain.active_claim
    if claim is None or domain.is_deleted:
        raise InvalidStatusTransition("Domain has no live ownership claim")
    claim.status = ClaimStatus.VERIFIED
    claim.verification_method = method
    claim.verified_at = now
    session.flush()
    record_event(
        session,
        domain,
        EventType.CLAIM_VERIFIED,
        {"claim_id": str(claim.id), "method": method},
        now=now,
    )
    return claim


def revoke_claim(
    session: Session, domain: Domain, *, reason: str, now: datetime | None = None
) -> OwnershipClaim | None:
    return _revoke_live_claim(session, domain, reason=reason, now=now or utcnow())


def _revoke_live_claim(
    session: Session, domain: Domain, *, reason: str, now: datetime
) -> OwnershipClaim | None:
    claim = domain.active_claim
    if claim is None:
        return None
    claim.status = ClaimStatus.REVOKED
    claim.revoked_at = now
    session.flush()
    record_event(
        session,
        domain,
        EventType.CLAIM_REVOKED,
        {"claim_id": str(claim.id), "reason": reason},
        now=now,
    )
    return claim


# --- reads ------------------------------------------------------------------


def _domain_query(include_deleted: bool):
    query = select(Domain).options(
        joinedload(Domain.application),
        selectinload(Domain.claims),
        selectinload(Domain.checks),
    )
    if not include_deleted:
        query = query.where(Domain.deleted_at.is_(None))
    return query


def get_domain(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> Domain:
    domain = session.scalar(
        _domain_query(include_deleted).where(
            Domain.id == domain_id, Domain.application_id == application.id
        )
    )
    if domain is None:
        raise DomainNotFound()
    return domain


def list_domains(
    session: Session,
    application: Application,
    *,
    reference: str | None = None,
    status: DomainStatus | None = None,
    include_deleted: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> list[Domain]:
    query = _domain_query(include_deleted).where(Domain.application_id == application.id)
    if reference is not None:
        query = query.where(Domain.reference == reference)
    if status is not None:
        query = query.where(Domain.status == status)
    query = query.order_by(Domain.created_at, Domain.id)
    query = query.limit(max(1, min(limit, MAX_PAGE_SIZE))).offset(max(0, offset))
    return list(session.scalars(query))


def find_live_by_hostname(session: Session, hostname: str) -> Domain | None:
    """Edge-side lookup: the single live row for a hostname, whatever its status."""
    canonical = canonicalize(hostname)
    return session.scalar(_domain_query(include_deleted=False).where(Domain.hostname == canonical))


def is_serveable(domain: Domain) -> bool:
    """Whether the edge may issue a certificate for and route this hostname.

    Evaluated on every call from current state: the owning application must
    be active, the domain live and ``ready``, the live claim verified and all
    four checks passing. A suspended tenant or a check that has started
    failing therefore stops service immediately.
    """
    if domain.is_deleted or domain.status != DomainStatus.READY:
        return False
    if domain.application is None or domain.application.status != ApplicationStatus.ACTIVE:
        return False
    claim = domain.active_claim
    if claim is None or claim.status != ClaimStatus.VERIFIED:
        return False
    return checks_passing(domain)


def checks_passing(domain: Domain) -> bool:
    by_type = {check.check_type: check for check in domain.checks}
    return all(
        check_type in by_type and by_type[check_type].status == CheckStatus.PASSING
        for check_type in CheckType
    )


# --- lifecycle --------------------------------------------------------------


def record_check(
    session: Session,
    domain: Domain,
    check_type: CheckType,
    status: CheckStatus,
    *,
    error_code: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    next_check_at: datetime | None = None,
) -> DomainCheck:
    observed_at = observed_at or utcnow()
    check = domain.check(check_type)
    if check is None:
        check = DomainCheck(domain_id=domain.id, check_type=check_type)
        session.add(check)
        domain.checks.append(check)
    previous = check.status
    check.status = status
    check.error_code = error_code if status == CheckStatus.FAILING else None
    check.message = message
    check.details = details
    check.observed_at = observed_at
    check.next_check_at = next_check_at
    session.flush()
    record_event(
        session,
        domain,
        EventType.CHECK_UPDATED,
        {
            "check": check_type.value,
            "from": previous.value if previous else None,
            "to": status.value,
            "error_code": check.error_code,
        },
        now=observed_at,
    )
    if status == CheckStatus.FAILING and domain.status == DomainStatus.READY:
        # A ready domain with a failing check is no longer ready; the
        # reconciliation worker (#9) decides recovery or suspension from here.
        transition_status(
            session,
            domain,
            DomainStatus.ATTENTION_REQUIRED,
            reason=f"{check_type.value}_check_failed",
            now=observed_at,
        )
    return check


def transition_status(
    session: Session,
    domain: Domain,
    new_status: DomainStatus,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> Domain:
    if domain.is_deleted:
        raise InvalidStatusTransition("Deleted domains cannot change status")
    if new_status == domain.status:
        return domain
    if new_status not in ALLOWED_TRANSITIONS[domain.status]:
        raise InvalidStatusTransition(
            f"Cannot move from {domain.status.value} to {new_status.value}"
        )
    if new_status == DomainStatus.READY:
        claim = domain.active_claim
        if claim is None or claim.status != ClaimStatus.VERIFIED:
            raise InvalidStatusTransition("Ownership claim is not verified")
        if not checks_passing(domain):
            raise InvalidStatusTransition("Every check must pass before the domain is ready")
    now = now or utcnow()
    previous = domain.status
    domain.status = new_status
    domain.updated_at = now
    session.flush()
    record_event(
        session,
        domain,
        EventType.STATUS_CHANGED,
        {"from": previous.value, "to": new_status.value, "reason": reason},
        now=now,
    )
    return domain


def delete_domain(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> Domain:
    """Tombstone the domain. Idempotent; the hostname is immediately claimable again."""
    now = now or utcnow()
    domain = get_domain(session, application, domain_id, include_deleted=True)
    if domain.is_deleted:
        return domain
    domain.status = DomainStatus.DELETING
    domain.deleted_at = now
    domain.purge_after = now + TOMBSTONE_RETENTION
    domain.updated_at = now
    session.flush()
    _revoke_live_claim(session, domain, reason="deleted", now=now)
    record_event(
        session,
        domain,
        EventType.DOMAIN_DELETED,
        {"hostname": domain.hostname, "purge_after": domain.purge_after.isoformat()},
        now=now,
    )
    return domain


def request_recheck(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> Domain:
    """Ask the lifecycle worker to re-run every check at the next opportunity.

    Sets ``next_check_at`` on all checks and records an event. Rate limiting
    of manual rechecks is applied by the API layer (#6, #12).
    """
    now = now or utcnow()
    domain = get_domain(session, application, domain_id)
    if domain.status == DomainStatus.DELETING:
        raise InvalidStatusTransition("Deleted domains are not rechecked")
    for check in domain.checks:
        check.next_check_at = now
    session.flush()
    record_event(session, domain, EventType.RECHECK_REQUESTED, {"requested_by": "api"}, now=now)
    return domain


def purge_tombstones(session: Session, *, now: datetime | None = None) -> int:
    """Hard-delete tombstones past retention. Children go with them via FK cascade."""
    now = now or utcnow()
    result = session.execute(
        delete(Domain).where(Domain.deleted_at.is_not(None), Domain.purge_after <= now)
    )
    session.flush()
    return result.rowcount or 0


# --- events -----------------------------------------------------------------


def record_event(
    session: Session,
    domain: Domain,
    event_type: EventType | str,
    payload: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> DomainEvent:
    event = DomainEvent(
        domain_id=domain.id,
        application_id=domain.application_id,
        event_type=str(event_type),
        payload=payload,
        created_at=now or utcnow(),
    )
    session.add(event)
    session.flush()
    return event


def list_events(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[DomainEvent]:
    domain = get_domain(session, application, domain_id, include_deleted=True)
    return list(
        session.scalars(
            select(DomainEvent)
            .where(DomainEvent.domain_id == domain.id)
            .order_by(DomainEvent.created_at, DomainEvent.id)
            .limit(max(1, min(limit, MAX_PAGE_SIZE)))
        )
    )
