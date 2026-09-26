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

import ipaddress
import socket
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.edge.config import EDGE_HEALTH_VALUE
from app.edge.probe import EdgeProbe, EdgeProbeFailed, WorkspaceProbe, probe_edge, probe_workspace
from app.edge.settings import HEALTH_PATH, EdgeSettings
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
from app.services.origin_verification import allow_private_from_env

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


def is_edge_name(session: Session, hostname: str, settings: EdgeSettings | None = None) -> bool:
    """Whether ``hostname`` is the edge's own name: EDGE_HOSTNAME or an application's target.

    The edge holds a certificate for its own names so that the health path
    answers over HTTPS there; that is how `custom-domain doctor` confirms
    that the name customers CNAME to reaches this edge and that issuance
    works. Only the health route matches such a request, so nothing is
    proxied for it. Former targets still named by live claims count too.
    """
    from sqlalchemy import select

    from app.hostname import InvalidHostname, canonicalize
    from app.models import Application, OwnershipClaim

    try:
        canonical = canonicalize(hostname, allow_apex=True)
    except InvalidHostname:
        return False
    if settings is not None and settings.edge_hostname == canonical:
        return True
    current = session.scalar(
        select(Application.id).where(
            Application.cname_target == canonical,
            Application.status == ApplicationStatus.ACTIVE,
        )
    )
    if current is not None:
        return True
    # A former target that a live claim still names (see
    # applications.edge_names) must keep its certificate until the last
    # domain issued against it has moved.
    former = session.scalar(
        select(OwnershipClaim.id)
        .join(Domain, Domain.id == OwnershipClaim.domain_id)
        .join(Application, Application.id == Domain.application_id)
        .where(
            OwnershipClaim.cname_target == canonical,
            OwnershipClaim.status != ClaimStatus.REVOKED,
            Domain.deleted_at.is_(None),
            Application.status == ApplicationStatus.ACTIVE,
        )
    )
    return former is not None


class EdgeProber(Protocol):
    def probe(self, hostname: str) -> EdgeProbe: ...

    def probe_workspace(self, hostname: str) -> WorkspaceProbe: ...


class SystemEdgeProber:
    """Probes through the public path (the hostname itself) or a fixed edge address."""

    def __init__(self, settings: EdgeSettings) -> None:
        self.settings = settings

    def _target(self, hostname: str) -> tuple[str, int]:
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
        # The customer controls where the hostname resolves. Without a fixed
        # edge address, only a public address may be probed, so a hostname
        # rebound to an internal address cannot turn the worker into a scanner.
        if (
            not self.settings.probe_address
            and not ipaddress.ip_address(address).is_global
            and not allow_private_from_env()
        ):
            raise EdgeProbeFailed(
                "edge_private_address",
                f"{host} resolves to the non-public address {address}; the edge is reached "
                "only at public addresses (or set EDGE_PROBE_ADDRESS to the edge itself).",
            )
        return address, port

    def probe_workspace(self, hostname: str) -> WorkspaceProbe:
        address, port = self._target(hostname)
        return probe_workspace(
            hostname,
            address=address,
            port=port,
            ca_file=self.settings.probe_ca_file,
            timeout=self.settings.probe_timeout,
            plain_http=self.settings.disable_https,
        )

    def probe(self, hostname: str) -> EdgeProbe:
        address, port = self._target(hostname)
        return probe_edge(
            hostname,
            address=address,
            port=port,
            ca_file=self.settings.probe_ca_file,
            timeout=self.settings.probe_timeout,
            health_path=HEALTH_PATH,
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
    # Warn at 7 days, or at a third of the lifetime for short-lived certificates
    # (Caddy renews at two thirds of the lifetime), so a 12-hour internal-CA
    # certificate is not reported as expiring the moment it is issued.
    warning = CERTIFICATE_EXPIRY_WARNING
    if getattr(probe, "not_before", None) is not None:
        warning = min(warning, (probe.not_after - probe.not_before) / 3)
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
    if remaining < warning:
        return Outcome(
            CheckStatus.FAILING,
            "certificate_expiring",
            f"The certificate for {domain.hostname} expires at {probe.not_after.isoformat()} "
            "and has not been renewed. Check Caddy's log for ACME errors, CAA records and rate "
            "limits.",
            details,
        )
    return Outcome(CheckStatus.PASSING, details=details)


def origin_outcome(
    domain: Domain, prober: EdgeProber | None = None, settings: EdgeSettings | None = None
) -> Outcome:
    """Does the application's origin serve the correct workspace for this hostname?

    Requires a verified active origin. When the application has the workspace
    probe enabled (the default), the origin must also answer
    ``GET /.well-known/custom-domain-workspace`` through the edge with the
    workspace reference it derived from the assertion; that proves routing,
    assertion verification and tenant selection end to end.
    """
    application = domain.application
    origin = application.serving_origin if application else None
    if origin is None:
        return Outcome(
            CheckStatus.FAILING,
            "origin_not_ready",
            f"Application {application.slug if application else '?'} has no verified active "
            "origin. Run `custom-domain origin verify --activate` for it.",
        )
    details: dict = {"origin": origin.url}
    if not application.workspace_probe_enabled or prober is None:
        details["workspace_probe"] = "disabled"
        return Outcome(CheckStatus.PASSING, details=details)
    try:
        probe = prober.probe_workspace(domain.hostname)
    except EdgeProbeFailed as exc:
        return Outcome(CheckStatus.FAILING, exc.code, exc.message, details)
    details["address"] = probe.address
    if probe.status != 200:
        return Outcome(
            CheckStatus.FAILING,
            "workspace_probe_failed",
            f"{domain.hostname} answered HTTP {probe.status} on the workspace path; the origin "
            "must return 200 with the workspace it selected from the assertion "
            "(docs/lifecycle.md).",
            details,
        )
    if probe.reference is None:
        return Outcome(
            CheckStatus.FAILING,
            "workspace_probe_invalid",
            f"{domain.hostname}: the workspace path returned 200 without a JSON `reference` "
            "field; the origin must echo the reference from the verified assertion.",
            details,
        )
    if probe.reference != domain.reference or (
        probe.application_id is not None and probe.application_id != str(domain.application_id)
    ):
        return Outcome(
            CheckStatus.FAILING,
            "workspace_mismatch",
            f"{domain.hostname} is served for workspace {probe.reference!r} but is registered "
            f"for {domain.reference!r}; the origin must select the workspace from the "
            "assertion's `ref`, never from the hostname or other input.",
            {**details, "observed_reference": probe.reference},
        )
    details["workspace"] = probe.reference
    return Outcome(CheckStatus.PASSING, details=details)


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
        origin_outcome(domain, prober, settings),
        now=now,
    )
