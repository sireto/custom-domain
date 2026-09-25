"""Ownership and routing checks from DNS, and the status rules they drive.

Ownership: the TXT record named in the live claim must contain the claim's
value. Routing: the hostname's CNAME chain must pass through the
application's CNAME target. Both are evaluated from public DNS with bounded
retries and backoff; every observation is recorded on the domain's checks
with the time of the next attempt.

Status rules (see docs/dns-verification.md):

* ``pending_dns`` or ``suspended`` and both checks pass: claim verified,
  domain moves to ``provisioning`` (certificate and origin checks follow in
  #7 and #9).
* ``provisioning`` and either check fails: back to ``pending_dns``.
* ``ready`` and either check fails: ``attention_required`` (done by
  ``record_check``); when both pass again and every other check still
  passes, back to ``ready``.
* ``attention_required`` with ownership failing for longer than
  ``OWNERSHIP_LOSS_GRACE`` on a verified claim: ``suspended``. Service only
  resumes after both checks pass again.

Tokens are never reused: a TXT value from a revoked claim is reported as
stale, and a hostname re-registered by another application gets a new token,
so records left behind by a previous owner cannot verify the new claim and
records of the new owner cannot revive the old one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.dns.resolver import DnsNameNotFound, DnsUnavailable, Resolver, cname_chain
from app.models import (
    CheckStatus,
    CheckType,
    ClaimStatus,
    Domain,
    DomainCheck,
    DomainStatus,
)
from app.models.types import utcnow
from app.services.domains import (
    checks_passing,
    mark_claim_verified,
    record_check,
    transition_status,
)

# Retry schedule while a check is failing or pending; the last value repeats.
BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(hours=1),
)
# How often passing checks are revalidated to catch drift.
REVALIDATE_INTERVAL = timedelta(hours=6)
# How long ownership may fail on a verified claim before service is suspended.
OWNERSHIP_LOSS_GRACE = timedelta(hours=24)
MAX_OBSERVED = 5


@dataclass(frozen=True)
class Outcome:
    status: CheckStatus
    error_code: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passing(self) -> bool:
        return self.status == CheckStatus.PASSING


def _observed(values: list[str]) -> list[str]:
    return [v[:120] for v in values[:MAX_OBSERVED]]


def ownership_outcome(resolver: Resolver, domain: Domain) -> Outcome:
    claim = domain.active_claim
    if claim is None:
        return Outcome(
            CheckStatus.FAILING, "claim_revoked", "The domain has no live ownership claim"
        )
    name = claim.txt_record_name
    try:
        values = resolver.txt(name)
    except DnsNameNotFound:
        return Outcome(
            CheckStatus.FAILING,
            "txt_record_not_found",
            f"No TXT record named {name} was found. Create it with the value shown in the "
            "domain's DNS records; propagation can take up to the record's TTL.",
        )
    except DnsUnavailable as exc:
        return Outcome(CheckStatus.FAILING, "dns_timeout", f"DNS lookup for {name} failed: {exc}")
    if not values:
        return Outcome(
            CheckStatus.FAILING,
            "txt_record_not_found",
            f"{name} exists but has no TXT record. Add a TXT record with the value shown in "
            "the domain's DNS records.",
        )
    expected = claim.txt_record_value
    if any(v.strip() == expected for v in values):
        return Outcome(CheckStatus.PASSING, details={"observed": _observed(values)})
    stale = {c.txt_record_value for c in domain.claims if c.status == ClaimStatus.REVOKED}
    if any(v.strip() in stale for v in values):
        return Outcome(
            CheckStatus.FAILING,
            "txt_token_stale",
            f"{name} holds a token from a previous registration of this hostname. Replace it "
            "with the current value shown in the domain's DNS records.",
            {"observed": _observed(values)},
        )
    return Outcome(
        CheckStatus.FAILING,
        "txt_token_mismatch",
        f"{name} has TXT records, but none is this registration's value. Check that the "
        "value was copied exactly, without quotes or extra text.",
        {"observed": _observed(values)},
    )


def routing_outcome(resolver: Resolver, domain: Domain) -> Outcome:
    claim = domain.active_claim
    if claim is None:
        return Outcome(
            CheckStatus.FAILING, "claim_revoked", "The domain has no live ownership claim"
        )
    target = claim.cname_target
    try:
        chain = cname_chain(resolver, domain.hostname, stop_at=target)
    except DnsNameNotFound:
        return Outcome(
            CheckStatus.FAILING,
            "cname_not_found",
            f"{domain.hostname} does not exist in DNS. Add a CNAME record pointing it at {target}.",
        )
    except DnsUnavailable as exc:
        return Outcome(
            CheckStatus.FAILING, "dns_timeout", f"DNS lookup for {domain.hostname} failed: {exc}"
        )
    if not chain:
        try:
            has_address = resolver.has_address(domain.hostname)
        except (DnsNameNotFound, DnsUnavailable):
            has_address = False
        if has_address:
            return Outcome(
                CheckStatus.FAILING,
                "cname_not_found",
                f"{domain.hostname} has A or AAAA records instead of a CNAME. Remove them and "
                f"add a CNAME record pointing at {target}.",
            )
        return Outcome(
            CheckStatus.FAILING,
            "cname_not_found",
            f"{domain.hostname} has no CNAME record. Add one pointing at {target}.",
        )
    if target in chain:
        return Outcome(CheckStatus.PASSING, details={"chain": chain})
    return Outcome(
        CheckStatus.FAILING,
        "cname_target_mismatch",
        f"{domain.hostname} points at {chain[-1]}, expected {target}. Update the CNAME "
        "record, and make sure no CDN or proxy rewrites it.",
        {"chain": chain},
    )


def schedule_next(
    check: DomainCheck | None, outcome: Outcome, now: datetime
) -> tuple[datetime, dict]:
    previous = dict(check.details or {}) if check is not None else {}
    details: dict[str, Any] = dict(outcome.details)
    if outcome.passing:
        details["attempts"] = 0
        return now + REVALIDATE_INTERVAL, details
    attempts = int(previous.get("attempts", 0)) + 1
    details["attempts"] = attempts
    details["failing_since"] = previous.get("failing_since") or now.isoformat()
    return now + BACKOFF[min(attempts, len(BACKOFF)) - 1], details


def apply_dns_outcomes(
    session: Session,
    domain: Domain,
    ownership: Outcome,
    routing: Outcome,
    *,
    now: datetime | None = None,
) -> tuple[DomainCheck, DomainCheck]:
    """Record both outcomes, schedule the next attempt and apply the status rules."""
    now = now or utcnow()
    recorded = []
    for check_type, outcome in ((CheckType.OWNERSHIP, ownership), (CheckType.ROUTING, routing)):
        existing = domain.check(check_type)
        next_at, details = schedule_next(existing, outcome, now)
        recorded.append(
            record_check(
                session,
                domain,
                check_type,
                outcome.status,
                error_code=outcome.error_code,
                message=outcome.message,
                details=details,
                observed_at=now,
                next_check_at=next_at,
            )
        )
    ownership_check, routing_check = recorded

    claim = domain.active_claim
    if ownership.passing and routing.passing:
        if claim is not None and claim.status == ClaimStatus.PENDING:
            mark_claim_verified(session, domain, method="dns_txt", now=now)
        if domain.status in (DomainStatus.PENDING_DNS, DomainStatus.SUSPENDED):
            transition_status(
                session, domain, DomainStatus.PROVISIONING, reason="dns_verified", now=now
            )
        elif domain.status == DomainStatus.ATTENTION_REQUIRED and checks_passing(domain):
            transition_status(session, domain, DomainStatus.READY, reason="recovered", now=now)
        return ownership_check, routing_check

    failed = "ownership" if not ownership.passing else "routing"
    if domain.status == DomainStatus.PROVISIONING:
        transition_status(
            session, domain, DomainStatus.PENDING_DNS, reason=f"{failed}_check_failed", now=now
        )
    elif (
        domain.status == DomainStatus.ATTENTION_REQUIRED
        and not ownership.passing
        and claim is not None
        and claim.status == ClaimStatus.VERIFIED
    ):
        since = datetime.fromisoformat(ownership_check.details["failing_since"])
        if now - since >= OWNERSHIP_LOSS_GRACE:
            transition_status(
                session, domain, DomainStatus.SUSPENDED, reason="ownership_lost", now=now
            )
    return ownership_check, routing_check


def run_dns_checks(
    session: Session, domain: Domain, resolver: Resolver, *, now: datetime | None = None
) -> tuple[DomainCheck, DomainCheck]:
    """Evaluate and apply both DNS checks for one live domain."""
    now = now or utcnow()
    return apply_dns_outcomes(
        session,
        domain,
        ownership_outcome(resolver, domain),
        routing_outcome(resolver, domain),
        now=now,
    )
