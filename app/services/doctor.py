"""Deployment health checks behind ``custom-domain doctor``.

Each check answers one question an operator asks on the first day: can the
API reach its database and are the migrations applied, is the edge gateway
reachable, has a reconciler run recently (is the worker container up), can
the edge reach the certificate authority, do the applications have a
verified origin, and does traffic for their CNAME target arrive at this
edge. Network calls are injectable so the checks are testable offline.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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
# Parsed JSON body of a GET; raises on failure.
FetchJson = Callable[[str], Any]
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


def _fetch_json(url: str) -> Any:
    import httpx

    response = httpx.get(url, timeout=8.0)
    response.raise_for_status()
    return response.json()


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
    fetch_json: FetchJson = _fetch_json,
    now: datetime | None = None,
) -> list[Finding]:
    now = now or utcnow()
    findings: list[Finding] = []
    findings.extend(_database_findings(session_factory))
    findings.extend(_settings_findings(settings, dns_settings))
    findings.extend(_edge_findings(settings, http_get))
    database_ok = not any(f.check == "database" and f.status == "fail" for f in findings)
    gateway_ok = any(f.check == "edge gateway" and f.ok for f in findings)
    if database_ok and gateway_ok:
        findings.extend(_edge_config_findings(session_factory, settings, fetch_json))
    if database_ok:
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
    portal = os.environ.get("PORTAL_PASSWORD", "").strip()
    if not portal:
        findings.append(Finding("portal", "warn", "disabled: set PORTAL_PASSWORD to enable it"))
    elif settings.portal_allowed_ips:
        findings.append(
            Finding(
                "portal",
                "ok",
                f"exposed at https://<edge name>/portal to {', '.join(settings.portal_ranges())}",
            )
        )
    else:
        findings.append(Finding("portal", "ok", "tunnel only (PORTAL_ALLOWED_IPS is empty)"))
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


def _edge_config_findings(
    session_factory: Callable[[], Session], settings: EdgeSettings, fetch_json: FetchJson
) -> list[Finding]:
    """Is the edge running the configuration this instance derives?

    The reconciler in the api and worker containers builds it from the
    database and these settings; the gateway in the edge container accepts
    it only when its own EDGE_ASK_URL, EDGE_ASSERT_UPSTREAM, ACME_EMAIL and
    EDGE_TLS_ISSUER produce the same TLS block and subrequest. A difference
    here means every reconciliation is being rejected (the worker log says
    why) or none has run yet.
    """
    from app.edge.config import build_apps, config_digest

    url = f"{settings.admin_url}/config/apps"
    try:
        running = fetch_json(url)
    except Exception as exc:
        return [Finding("edge config", "warn", f"could not read {url}: {type(exc).__name__}")]
    with session_factory() as session:
        desired = build_apps(session, settings)
    desired_digest = config_digest(desired)
    running_digest = config_digest(running) if isinstance(running, dict) else "none"
    if running_digest == desired_digest:
        return [
            Finding(
                "edge config", "ok", f"the edge runs the desired configuration ({desired_digest})"
            )
        ]
    running_routes = []
    if isinstance(running, dict):
        server = running.get("http", {}).get("servers", {}).get("edge", {})
        running_routes = [
            str(r.get("@id")) for r in server.get("routes", []) if isinstance(r, dict)
        ]
    return [
        Finding(
            "edge config",
            "fail",
            f"the edge runs a different configuration (routes {running_routes}, "
            f"{'with' if isinstance(running, dict) and running.get('tls') else 'without'} TLS "
            f"automation) than this instance derives ({desired_digest}): the gateway is "
            "rejecting the reconciler's configuration or it has not run. Check the worker "
            "log for config_rejected; EDGE_ASK_URL, EDGE_ASSERT_UPSTREAM, ACME_EMAIL and "
            "EDGE_TLS_ISSUER must be identical for the api, worker and edge containers",
        )
    ]


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
    if settings.edge_hostname:
        findings.extend(
            _cname_target_findings(
                None, settings.edge_hostname, settings, dns_settings, resolve, probe_target
            )
        )
    else:
        findings.append(
            Finding(
                "edge hostname",
                "warn",
                "EDGE_HOSTNAME is not set: the edge has no name of its own until an application "
                "exists, so the portal is reachable only over the tunnel until then",
            )
        )
    with session_factory() as session:
        applications = session.scalars(
            select(Application).where(Application.deleted_at.is_(None)).order_by(Application.slug)
        ).all()
        if not applications:
            findings.append(
                Finding(
                    "applications",
                    "warn",
                    "none yet: create one with `custom-domain application create`",
                )
            )
            return findings
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
                if target == settings.edge_hostname:
                    continue  # already checked as the edge hostname
                findings.extend(
                    _cname_target_findings(
                        application, target, settings, dns_settings, resolve, probe_target
                    )
                )
    return findings


def _publicly_routable(address) -> bool:
    """The standard global-address classification (RFC 6890 and the IANA registries).

    Loopback, link-local, private, shared address space (100.64.0.0/10),
    benchmarking, documentation and reserved ranges are all non-global:
    customers on the Internet cannot route to them, so a record answering
    with one is a misconfiguration whatever the reason.
    """
    return bool(address.is_global)


def _cname_target_findings(
    application: Application | None,
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

    if application is None:
        check = f"edge hostname {target}"
    else:
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
    unverifiable: list[str] = []
    reached: list[str] = []
    for address in addresses:
        try:
            status, marker = probe_target(target, address, settings)
        except Exception as exc:
            if _unverifiable_from_here(address, exc):
                # Docker networks carry no IPv6 by default, so an IPv6 address
                # this container cannot route to says nothing about the edge.
                # An unroutable IPv4 address is a real failure.
                unverifiable.append(address)
            else:
                problems.append(f"{address}: {type(exc).__name__}: {exc}")
            continue
        if status == 204 and marker == EDGE_HEALTH_VALUE:
            reached.append(address)
        else:
            problems.append(f"{address}: HTTP {status} without this edge's marker")
    if not problems and not unverifiable:
        return [Finding(check, "ok", f"{', '.join(addresses)} all answer {url} as this edge")]
    if not problems:
        return [
            Finding(
                check,
                "warn",
                f"{', '.join(reached)} answer {url} as this edge; the IPv6 address(es) "
                f"{', '.join(unverifiable)} could not be checked from this container (no "
                "IPv6 route here, which is normal inside Docker). Verify from outside: "
                f"curl -6 -I {url}",
            )
        ]
    if unverifiable:
        problems.append(f"{', '.join(unverifiable)}: not checkable from this container")
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


def check_edge_name(
    name: str,
    settings: EdgeSettings,
    dns_settings: DnsSettings,
    *,
    resolve: Resolve = _resolve,
    probe_target: ProbeTarget = _probe_target,
) -> Finding:
    """The doctor's check for one of the edge's names: public DNS, then each address."""
    return _cname_target_findings(None, name, settings, dns_settings, resolve, probe_target)[0]


def _unverifiable_from_here(address: str, exc: BaseException) -> bool:
    """An IPv6 address this container has no route to: the one case that is
    an environment fact rather than a verdict on the edge."""
    import ipaddress

    try:
        if ipaddress.ip_address(address).version != 6:
            return False
    except ValueError:
        return False
    return _no_route(exc)


def _no_route(exc: BaseException) -> bool:
    """Whether a probe failed because this host has no route to the address at all."""
    import errno

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in (
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
        ):
            return True
        current = current.__cause__ or current.__context__
    return "Network is unreachable" in str(exc) or "No route to host" in str(exc)


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
