"""Certificate and origin checks, certificate authorization, and readiness.

* ``certificate_authorized`` is the rule behind Caddy's on-demand TLS ask
  endpoint: issue or renew a certificate only for a live hostname whose
  ownership claim is verified, whose application is active, and whose
  domain is not waiting for DNS, suspended or deleted. It is a single indexed
  lookup; it never queries DNS.
* The certificate check runs the readiness probe through the edge; the
  origin check requires the application to have a verified active origin.
* A ``provisioning`` domain becomes ``ready`` only when all four checks pass
  and the claim is verified; an ``attention_required`` domain returns to
  ``ready`` the same way.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.edge.config import EDGE_HEALTH_VALUE
from app.edge.probe import EdgeProbe, EdgeProbeFailed, probe_edge
from app.edge.settings import EdgeSettings
from app.models import (
    ApplicationStatus,
    CheckStatus,
    CheckType,
    ClaimStatus,
    Domain,
    DomainCheck,
    DomainStatus,
)
from app.models.types import utcnow
from app.services.dns_checks import Outcome, schedule_next
from app.services.domains import checks_passing, record_check, transition_status

CERTIFICATE_EXPIRY_WARNING = timedelta(days=7)
EDGE_CHECK_TYPES = (CheckType.CERTIFICATE, CheckType.ORIGIN)
NOT_ISSUABLE = (DomainStatus.PENDING_DNS, DomainStatus.SUSPENDED, DomainStatus.DELETING)


def certificate_authorized(domain: Domain | None) -> bool:
    """Whether the edge may obtain or renew a certificate for this domain."""
    if domain is None or domain.is_deleted or domain.status in NOT_ISSUABLE:
        return False
    if domain.application is None or domain.application.status != ApplicationStatus.ACTIVE:
        return False
    claim = domain.active_claim
    return claim is not None and claim.status == ClaimStatus.VERIFIED


def eligible_for_edge_checks(domain: Domain) -> bool:
    return certificate_authorized(domain)


class EdgeProber(Protocol):
    def probe(self, hostname: str) -> EdgeProbe: ...


class SystemEdgeProber:
    """Probes through the public path (the hostname itself) or a fixed edge address."""

    def __init__(self, settings: EdgeSettings) -> None:
        self.settings = settings

    def probe(self, hostname: str) -> EdgeProbe:
        if self.settings.probe_address:
            host, _, port_text = self.settings.probe_address.rpartition(":")
            port = int(port_text)
        else:
            host, port = hostname, self.settings.https_port
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise EdgeProbeFailed(
                "edge_unresolvable", f"{host} does not resolve, so the edge cannot be probed: {exc}"
            ) from exc
        if not infos:
            raise EdgeProbeFailed("edge_unresolvable", f"{host} has no addresses")
        address = infos[0][4][0]
        return probe_edge(
            hostname,
            address=address,
            port=port,
            ca_file=self.settings.probe_ca_file,
            timeout=self.settings.probe_timeout,
            health_path=self.settings.health_path,
        )


def certificate_outcome(
    prober: EdgeProber, domain: Domain, settings: EdgeSettings, *, now: datetime | None = None
) -> Outcome:
    now = now or utcnow()
    if settings.disable_https:
        return Outcome(CheckStatus.PASSING, details={"tls": "disabled"})
    try:
        probe = prober.probe(domain.hostname)
    except EdgeProbeFailed as exc:
        return Outcome(CheckStatus.FAILING, exc.code, exc.message)
    if probe.status != 204 or probe.edge_header != EDGE_HEALTH_VALUE:
        return Outcome(
            CheckStatus.FAILING,
            "edge_not_reached",
            f"{domain.hostname} answered over HTTPS (HTTP {probe.status}) but not from this edge; "
            "traffic is reaching another server. Check the CNAME target and any proxy or CDN "
            "in front of the hostname.",
            {"address": probe.address},
        )
    remaining = probe.not_after - now
    details = {
        "address": probe.address,
        "issuer": probe.issuer,
        "not_after": probe.not_after.isoformat(),
    }
    if remaining <= timedelta(0):
        return Outcome(
            CheckStatus.FAILING,
            "certificate_expired",
            f"The certificate for {domain.hostname} expired at {probe.not_after.isoformat()}; "
            "renewal has failed. Check Caddy's log for ACME errors, CAA records and rate limits.",
            details,
        )
    if remaining < CERTIFICATE_EXPIRY_WARNING:
        return Outcome(
            CheckStatus.FAILING,
            "certificate_expiring",
            f"The certificate for {domain.hostname} expires at {probe.not_after.isoformat()} "
            "and has not been renewed. Check Caddy's log for ACME errors, CAA records and rate "
            "limits.",
            details,
        )
    return Outcome(CheckStatus.PASSING, details=details)


def origin_outcome(domain: Domain) -> Outcome:
    origin = domain.application.serving_origin if domain.application else None
    if origin is None:
        return Outcome(
            CheckStatus.FAILING,
            "origin_not_ready",
            f"Application {domain.application.slug if domain.application else '?'} has no "
            "verified active origin. Run `custom-domain origin verify --activate` for it.",
        )
    return Outcome(CheckStatus.PASSING, details={"origin": origin.url})


def apply_edge_outcomes(
    session: Session,
    domain: Domain,
    certificate: Outcome,
    origin: Outcome,
    *,
    now: datetime | None = None,
) -> tuple[DomainCheck, DomainCheck]:
    now = now or utcnow()
    recorded = []
    for check_type, outcome in ((CheckType.CERTIFICATE, certificate), (CheckType.ORIGIN, origin)):
        next_at, details = schedule_next(domain.check(check_type), outcome, now)
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
    claim = domain.active_claim
    if (
        domain.status in (DomainStatus.PROVISIONING, DomainStatus.ATTENTION_REQUIRED)
        and claim is not None
        and claim.status == ClaimStatus.VERIFIED
        and checks_passing(domain)
    ):
        reason = (
            "readiness_probe_passed" if domain.status == DomainStatus.PROVISIONING else "recovered"
        )
        transition_status(session, domain, DomainStatus.READY, reason=reason, now=now)
    return recorded[0], recorded[1]


def run_edge_checks(
    session: Session,
    domain: Domain,
    prober: EdgeProber,
    settings: EdgeSettings,
    *,
    now: datetime | None = None,
) -> tuple[DomainCheck, DomainCheck]:
    now = now or utcnow()
    return apply_edge_outcomes(
        session,
        domain,
        certificate_outcome(prober, domain, settings, now=now),
        origin_outcome(domain),
        now=now,
    )
