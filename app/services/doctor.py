"""Deployment health checks behind ``custom-domain doctor``.

Each check answers one question an operator asks on the first day: can the
API reach its database and are the migrations applied, is the edge gateway
reachable, has a reconciler run recently (is the worker container up), can
the edge reach the certificate authority, do the applications have a
verified origin, and does traffic for their CNAME target arrive at this
edge. Network calls are injectable so the checks are testable offline.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.dns.settings import DnsSettings
from app.edge.config import EDGE_HEALTH_VALUE
from app.edge.settings import HEALTH_PATH, EdgeSettings
from app.models import Application, ApplicationStatus, EdgeLock
from app.models.types import utcnow

ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
MIN_TOKEN_LENGTH = 32

# (status, headers) of a GET without following redirects; raises on failure.
HttpGet = Callable[[str], tuple[int, dict[str, str]]]
# Public addresses of a name, looked up in public DNS (never the local resolver).
Resolve = Callable[[str, DnsSettings], list[str]]
# (status, edge marker) of the health path at ``address`` for ``hostname``; raises on failure.
ProbeTarget = Callable[[str, str, EdgeSettings], tuple[int, str | None]]
PUBLIC_NAMESERVERS = ("1.1.1.1", "8.8.8.8")


@dataclass(frozen=True)
class Finding:
    check: str
    status: str  # "ok", "warn" or "fail"
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _http_get(url: str) -> tuple[int, dict[str, str]]:
    import httpx

    response = httpx.get(url, timeout=8.0, follow_redirects=False)
    return response.status_code, dict(response.headers)


def _resolve(host: str, dns_settings: DnsSettings) -> list[str]:
    """A and AAAA records of ``host`` from public DNS.

    The local resolver is deliberately not used: inside the API container it
    is the host's resolver, and a host named after the edge (``edge.example``
    as the machine's hostname) answers its own name with 127.0.1.1, which
    is not what customers' CNAMEs will reach.
    """
    import dns.resolver

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(dns_settings.nameservers or PUBLIC_NAMESERVERS)
    resolver.timeout = resolver.lifetime = dns_settings.timeout
    addresses: list[str] = []
    for rdtype in ("A", "AAAA"):
        try:
            answer = resolver.resolve(host, rdtype, raise_on_no_answer=False, search=False)
        except dns.resolver.NXDOMAIN as exc:
            raise LookupError(f"{host} does not exist in public DNS") from exc
        if answer.rrset is not None:
            addresses.extend(str(record) for record in answer.rrset)
    return addresses


def _probe_target(hostname: str, address: str, settings: EdgeSettings) -> tuple[int, str | None]:
    """Fetch the health path at ``address`` presenting ``hostname``, as a customer would."""
    import http.client

    from app.edge.config import EDGE_HEALTH_HEADER
    from app.edge.probe import probe_edge

    if not settings.disable_https:
        probe = probe_edge(
            hostname,
            address=address,
            port=settings.https_port,
            ca_file=settings.probe_ca_file,
            timeout=settings.probe_timeout,
        )
        return probe.status, probe.edge_header
    sock = socket.create_connection((address, settings.https_port), timeout=settings.probe_timeout)
    try:
        connection = http.client.HTTPConnection(
            hostname, settings.https_port, timeout=settings.probe_timeout
        )
        connection.sock = sock
        connection.request("GET", HEALTH_PATH, headers={"Host": hostname})
        response = connection.getresponse()
        response.read(1024)
        return response.status, response.getheader(EDGE_HEALTH_HEADER)
    finally:
        sock.close()


def run_doctor(
    session_factory: Callable[[], Session],
    settings: EdgeSettings,
    dns_settings: DnsSettings,
    *,
    http_get: HttpGet = _http_get,
    resolve: Resolve = _resolve,
    probe_target: ProbeTarget = _probe_target,
    now: datetime | None = None,
) -> list[Finding]:
    now = now or utcnow()
    findings: list[Finding] = []
    findings.extend(_database_findings(session_factory))
    findings.extend(_settings_findings(settings, dns_settings))
    findings.extend(_edge_findings(settings, http_get))
    if not any(f.check == "database" and f.status == "fail" for f in findings):
        findings.extend(_reconciler_findings(session_factory, settings, now))
        findings.extend(
            _application_findings(session_factory, settings, dns_settings, resolve, probe_target)
        )
    return findings


def _database_findings(session_factory: Callable[[], Session]) -> list[Finding]:
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            engine = session.get_bind()
    except Exception as exc:
        return [Finding("database", "fail", f"cannot connect: {type(exc).__name__}: {exc}")]
    findings = [Finding("database", "ok", "reachable")]
    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        from app.db.migrate import alembic_config

        script = ScriptDirectory.from_config(alembic_config(str(engine.url)))
        heads = set(script.get_heads())
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
        if current == heads:
            findings.append(Finding("migrations", "ok", f"at {', '.join(sorted(heads))}"))
        else:
            findings.append(
                Finding(
                    "migrations",
                    "fail",
                    f"database is at {', '.join(sorted(current)) or 'nothing'}, code expects "
                    f"{', '.join(sorted(heads))}; run `custom-domain db upgrade`",
                )
            )
    except Exception as exc:
        findings.append(Finding("migrations", "warn", f"could not compare revisions: {exc}"))
    return findings


def _settings_findings(settings: EdgeSettings, dns_settings: DnsSettings) -> list[Finding]:
    findings: list[Finding] = []
    if settings.legacy_api_enabled:
        findings.append(
            Finding(
                "legacy api",
                "warn",
                "ENABLE_LEGACY_API is true: the edge reconciler is off and the deprecated "
                "/domains endpoint is served; set it to false once hostnames are imported",
            )
        )
    if not settings.edge_token:
        findings.append(
            Finding(
                "edge token",
                "warn",
                "EDGE_TOKEN is not set: the internal endpoints rely on the address check alone",
            )
        )
    elif len(settings.edge_token) < MIN_TOKEN_LENGTH:
        findings.append(
            Finding("edge token", "warn", f"EDGE_TOKEN is shorter than {MIN_TOKEN_LENGTH} chars")
        )
    else:
        findings.append(Finding("edge token", "ok", "set"))
    if dns_settings.verification_mode == "local":
        findings.append(
            Finding(
                "dns mode",
                "warn",
                "DNS_VERIFICATION_MODE=local: the DNS checks are not verifying public DNS "
                "(development only)",
            )
        )
    if settings.disable_https:
        findings.append(Finding("https", "warn", "DISABLE_HTTPS=true: the edge serves plain HTTP"))
    elif settings.tls_issuer == "internal":
        findings.append(
            Finding("https", "warn", "EDGE_TLS_ISSUER=internal: certificates from a private CA")
        )
    else:
        account = (
            f", ACME account {settings.acme_email}"
            if settings.acme_email
            else " (no ACME_EMAIL set)"
        )
        findings.append(Finding("https", "ok", f"public certificates{account}"))
    return findings


def _edge_findings(settings: EdgeSettings, http_get: HttpGet) -> list[Finding]:
    findings: list[Finding] = []
    if not settings.reconcile_enabled:
        return findings
    url = f"{settings.admin_url}/config/apps"
    try:
        status, _ = http_get(url)
    except Exception as exc:
        findings.append(
            Finding(
                "edge gateway",
                "fail",
                f"{url} unreachable ({type(exc).__name__}): is the edge container up and "
                "CADDY_ADMIN_URL pointing at it?",
            )
        )
        return findings
    if status == 200:
        findings.append(Finding("edge gateway", "ok", f"{url} answers"))
    else:
        findings.append(Finding("edge gateway", "fail", f"{url} answered HTTP {status}"))
    if not settings.disable_https and settings.tls_issuer == "acme":
        try:
            status, _ = http_get(ACME_DIRECTORY)
            if status == 200:
                findings.append(Finding("acme", "ok", "Let's Encrypt reachable"))
            else:
                findings.append(Finding("acme", "warn", f"{ACME_DIRECTORY} answered HTTP {status}"))
        except Exception as exc:
            findings.append(
                Finding(
                    "acme",
                    "fail",
                    f"cannot reach {ACME_DIRECTORY} ({type(exc).__name__}): certificates "
                    "cannot be issued without outbound HTTPS",
                )
            )
    return findings


def _reconciler_findings(
    session_factory: Callable[[], Session], settings: EdgeSettings, now: datetime
) -> list[Finding]:
    if not settings.reconcile_enabled:
        return []
    with session_factory() as session:
        lock = session.scalar(select(EdgeLock).where(EdgeLock.name == "reconcile"))
        locked_at = lock.locked_at if lock is not None else None
        if locked_at is not None and locked_at.tzinfo is None:
            from datetime import UTC

            locked_at = locked_at.replace(tzinfo=UTC)
    allowance = timedelta(seconds=3 * settings.reconcile_interval + 60)
    if locked_at is None:
        return [
            Finding(
                "reconciler",
                "warn",
                "no reconciliation has run yet: is the worker container running?",
            )
        ]
    age = now - locked_at
    if age > allowance:
        return [
            Finding(
                "reconciler",
                "warn",
                f"last run {int(age.total_seconds())}s ago by {lock.holder or '?'}: "
                "is the worker container running?",
            )
        ]
    return [Finding("reconciler", "ok", f"last run {int(age.total_seconds())}s ago")]


def _application_findings(
    session_factory: Callable[[], Session],
    settings: EdgeSettings,
    dns_settings: DnsSettings,
    resolve: Resolve,
    probe_target: ProbeTarget,
) -> list[Finding]:
    findings: list[Finding] = []
    with session_factory() as session:
        applications = session.scalars(select(Application).order_by(Application.slug)).all()
        if not applications:
            return [
                Finding(
                    "applications",
                    "warn",
                    "none yet: create one with `custom-domain application create`",
                )
            ]
        for application in applications:
            if application.status != ApplicationStatus.ACTIVE:
                findings.append(Finding(f"application {application.slug}", "warn", "not active"))
                continue
            origin = application.serving_origin
            if origin is None:
                findings.append(
                    Finding(
                        f"application {application.slug}",
                        "warn",
                        "no verified active origin: register, verify and activate one",
                    )
                )
            else:
                findings.append(
                    Finding(f"application {application.slug}", "ok", f"origin {origin.url}")
                )
            from app.services.applications import edge_names

            for target in edge_names(session, application):
                findings.extend(
                    _cname_target_findings(
                        application, target, settings, dns_settings, resolve, probe_target
                    )
                )
    return findings


def _publicly_routable(address) -> bool:
    """Loopback, link-local, private (RFC 1918 / ULA) and unspecified addresses are not.

    Documentation ranges count as non-global in ``ipaddress`` but are left
    alone: they never come back from real DNS and they appear in tests.
    """
    import ipaddress

    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        return False
    private = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    return not any(address in net for net in private)


def _cname_target_findings(
    application: Application,
    target: str,
    settings: EdgeSettings,
    dns_settings: DnsSettings,
    resolve: Resolve,
    probe_target: ProbeTarget,
) -> list[Finding]:
    """Does the name customers CNAME to reach this edge?

    The name is looked up in public DNS and each address is asked for the
    health path with the name as SNI and Host, exactly as a customer's
    browser would; only this edge answers 204 with ``X-Custom-Domain-Edge:
    1``. Over HTTPS the edge obtains a certificate for its own name on the
    first handshake, so a fresh deployment may need a second run.
    """
    import ipaddress

    check = f"cname target {target}"
    if target != application.cname_target:
        check += " (former, still named by live claims)"
    try:
        addresses = resolve(target, dns_settings)
    except Exception as exc:
        return [
            Finding(
                check,
                "fail",
                f"public DNS lookup failed ({exc}): customers' CNAMEs point here, so it needs "
                "an A/AAAA record for this edge",
            )
        ]
    if not addresses:
        return [
            Finding(check, "fail", "has no A or AAAA record in public DNS: add one for this edge")
        ]
    private = [a for a in addresses if not _publicly_routable(ipaddress.ip_address(a))]
    if private:
        return [
            Finding(
                check,
                "fail",
                f"resolves to {', '.join(private)}, not a public address: customers cannot "
                "reach that; point the record at the edge's public address",
            )
        ]
    port = "" if settings.https_port in (80, 443) else f":{settings.https_port}"
    scheme = "http" if settings.disable_https else "https"
    url = f"{scheme}://{target}{port}{HEALTH_PATH}"
    problems: list[str] = []
    for address in addresses:
        try:
            status, marker = probe_target(target, address, settings)
        except Exception as exc:
            problems.append(f"{address}: {type(exc).__name__}: {exc}")
            continue
        if not (status == 204 and marker == EDGE_HEALTH_VALUE):
            problems.append(f"{address}: HTTP {status} without this edge's marker")
    if not problems:
        return [Finding(check, "ok", f"{', '.join(addresses)} all answer {url} as this edge")]
    return [
        Finding(
            check,
            "fail",
            f"{url} at {'; '.join(problems)}. Open TCP 80 and 443 (and UDP 443) to the edge "
            "for every published address, run doctor again in a minute if the certificate "
            "for this name is still being issued, and check the edge log for ACME errors "
            "if it keeps failing",
        )
    ]


def summarize(findings: list[Finding]) -> tuple[int, int, int]:
    """Counts of (ok, warn, fail)."""
    return (
        sum(1 for f in findings if f.status == "ok"),
        sum(1 for f in findings if f.status == "warn"),
        sum(1 for f in findings if f.status == "fail"),
    )


def as_rows(findings: list[Finding]) -> list[tuple[str, str, str]]:
    return [(f.status.upper(), f.check, f.detail) for f in findings]


__all__: list[str] = [
    "Finding",
    "run_doctor",
    "summarize",
    "as_rows",
    "ACME_DIRECTORY",
]
