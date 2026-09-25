"""Import domains from the volume-based deployment into the database.

The current service persists a full Caddy JSON config (``domains/caddy.json``)
with one route per hostname. This module reads that file and registers each
hostname under one application so an existing deployment can cut over without
customers re-adding their domains. See docs/data-model.md for the staged
cutover.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.hostname import InvalidHostname, canonicalize
from app.models import Application, CheckStatus, CheckType, Domain, DomainStatus, EventType
from app.models.types import utcnow
from app.services.domains import (
    claim_domain,
    find_live_by_hostname,
    mark_claim_verified,
    record_check,
    record_event,
    transition_status,
)
from app.services.errors import HostnameAlreadyClaimed

LEGACY_IMPORT_METHOD = "legacy_import"
MISSING_REFERENCE = "missing_reference"


@dataclass(frozen=True)
class LegacyDomain:
    hostname: str
    upstream: str | None


@dataclass
class ImportReport:
    """Outcome of one import run.

    ``imported`` were created by this run; ``existing`` were already
    registered by the same application (a re-run is a no-op for them);
    ``skipped`` could not be imported and carry a stable reason code.
    """

    imported: list[Domain] = field(default_factory=list)
    existing: list[Domain] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.skipped


def parse_legacy_config(config: Mapping[str, Any], *, port: int = 443) -> list[LegacyDomain]:
    """Extract (hostname, upstream) pairs from a saved Caddy JSON config."""
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    routes = servers.get(str(port), {}).get("routes", [])
    entries: list[LegacyDomain] = []
    for route in routes:
        upstream = _first_upstream(route)
        for match in route.get("match", []):
            for host in match.get("host", []):
                entries.append(LegacyDomain(hostname=host, upstream=upstream))
    return entries


def _first_upstream(node: Any) -> str | None:
    if isinstance(node, dict):
        if node.get("handler") == "reverse_proxy":
            upstreams = node.get("upstreams") or []
            if upstreams:
                return upstreams[0].get("dial")
        for value in node.values():
            found = _first_upstream(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _first_upstream(item)
            if found:
                return found
    return None


def import_legacy_domains(
    session: Session,
    application: Application,
    entries: Iterable[LegacyDomain],
    *,
    references: Mapping[str, str] | None = None,
    grandfather: bool = False,
    hostname_as_reference: bool = False,
    now: datetime | None = None,
) -> ImportReport:
    """Register each legacy hostname under ``application``.

    ``references`` maps each hostname to the application's workspace
    reference. The reference is tenant context for routing, so a hostname
    without a mapping is skipped with ``missing_reference`` unless
    ``hostname_as_reference`` is set, which is only correct for applications
    that resolve workspaces by hostname themselves.

    With ``grandfather`` the ownership claim is marked verified by import and
    the domain moves to ``provisioning``; routing, certificate and origin
    checks still have to pass before it becomes ``ready``. Without it the
    domain starts in ``pending_dns`` and the customer must publish the TXT
    record like any new registration.

    Hostnames already registered by the same application are reported as
    ``existing`` and left untouched, so re-running is safe.
    """
    now = now or utcnow()
    references = references or {}
    report = ImportReport()
    for entry in entries:
        try:
            hostname = canonicalize(entry.hostname)
        except InvalidHostname as exc:
            report.skipped.append((entry.hostname, exc.code))
            continue
        reference = references.get(hostname) or references.get(entry.hostname)
        if not reference and hostname_as_reference:
            reference = hostname
        if not reference:
            report.skipped.append((hostname, MISSING_REFERENCE))
            continue
        metadata = {"legacy_upstream": entry.upstream, "imported_at": now.isoformat()}
        try:
            domain = claim_domain(
                session, application, hostname, reference, metadata=metadata, now=now
            )
        except HostnameAlreadyClaimed:
            live = find_live_by_hostname(session, hostname)
            if live is not None and live.application_id == application.id:
                report.existing.append(live)
            else:
                report.skipped.append((hostname, HostnameAlreadyClaimed.code))
            continue
        record_event(
            session,
            domain,
            EventType.DOMAIN_IMPORTED,
            {"upstream": entry.upstream, "grandfathered": grandfather},
            now=now,
        )
        if grandfather:
            mark_claim_verified(session, domain, method=LEGACY_IMPORT_METHOD, now=now)
            record_check(
                session,
                domain,
                CheckType.OWNERSHIP,
                CheckStatus.PASSING,
                details={"source": LEGACY_IMPORT_METHOD},
                observed_at=now,
            )
            transition_status(
                session, domain, DomainStatus.PROVISIONING, reason=LEGACY_IMPORT_METHOD, now=now
            )
        report.imported.append(domain)
    return report
