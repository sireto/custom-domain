"""Derive the complete Caddy configuration from the database.

The output is a pure function of the authoritative state plus the edge
settings, so it can be rebuilt after any restart and compared against what
Caddy is running. Nothing here writes to the database.

Routing policy in this issue: a hostname is routed when ``is_serveable``
holds (application active, domain live and ``ready``, claim verified, all
checks passing) and its application has an active origin. Certificate
authorization for not-yet-ready hostnames (on-demand TLS) is added in #7 and
request headers plus the signed assertion in #8.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.edge.assertion import HEADER as ASSERTION_HEADER
from app.edge.settings import ASSERT_PATH, HEALTH_PATH, EdgeSettings
from app.models import (
    Application,
    ApplicationStatus,
    ClaimStatus,
    Domain,
    DomainStatus,
    VerifiedOrigin,
)
from app.services.origin_verification import (
    OriginVerificationFailed,
    allow_private_from_env,
    pinned_dial,
)

logger = logging.getLogger(__name__)

ROUTABLE_STATUSES = (
    DomainStatus.PROVISIONING,
    DomainStatus.READY,
    DomainStatus.ATTENTION_REQUIRED,
)


def routable(domain: Domain) -> bool:
    """Hostnames the edge lists in its routes.

    Routing is wider than serving: a verified, active domain is routed as soon
    as it is provisioning so the readiness probe can reach the origin through
    the edge. The assert step (app/v1/internal.py) admits ordinary requests
    only when the domain is ready, and only the workspace probe path before.
    """
    if domain.is_deleted or domain.status not in ROUTABLE_STATUSES:
        return False
    if domain.application is None or domain.application.status != ApplicationStatus.ACTIVE:
        return False
    claim = domain.active_claim
    return claim is not None and claim.status == ClaimStatus.VERIFIED


SERVER_NAME = "edge"
EDGE_HEALTH_HEADER = "X-Custom-Domain-Edge"
EDGE_HEALTH_VALUE = "1"
EDGE_HOST_HEADER = "X-Custom-Domain-Edge-Host"
EDGE_SNI_HEADER = "X-Custom-Domain-Edge-Sni"
EDGE_REQUEST_ID_HEADER = "X-Custom-Domain-Edge-Request-Id"
EDGE_TOKEN_HEADER = "X-Custom-Domain-Edge-Token"
# Headers a client must never be able to smuggle to an origin.
STRIPPED_REQUEST_HEADERS = (
    ASSERTION_HEADER,
    EDGE_HOST_HEADER,
    EDGE_SNI_HEADER,
    EDGE_REQUEST_ID_HEADER,
    EDGE_TOKEN_HEADER,
    "X-Custom-Domain-Reference",
    "X-Custom-Domain-Application",
)


@dataclass(frozen=True)
class RouteGroup:
    application_slug: str
    origin: str  # pinned address:port the edge dials
    origin_host: str  # the origin's name, presented as SNI and Host
    origin_tls: bool
    hostnames: tuple[str, ...]


def serveable_route_groups(session: Session) -> list[RouteGroup]:
    """Routable hostnames (see ``routable``), grouped per application, in a stable order."""
    applications = session.scalars(
        select(Application)
        .where(Application.status == ApplicationStatus.ACTIVE)
        .options(selectinload(Application.origins))
        .order_by(Application.slug)
    ).all()
    groups: list[RouteGroup] = []
    for application in applications:
        origin: VerifiedOrigin | None = application.serving_origin
        if origin is None:
            continue
        domains = session.scalars(
            select(Domain)
            .where(
                Domain.application_id == application.id,
                Domain.deleted_at.is_(None),
                Domain.status.in_(ROUTABLE_STATUSES),
            )
            .options(
                joinedload(Domain.application),
                selectinload(Domain.claims),
                selectinload(Domain.checks),
            )
            .order_by(Domain.hostname)
        ).all()
        hostnames = tuple(d.hostname for d in domains if routable(d))
        if not hostnames:
            continue
        try:
            dial, origin_host = pinned_dial(
                origin.host, origin.port, allow_private=allow_private_from_env()
            )
        except OriginVerificationFailed as exc:
            # The origin no longer resolves to an acceptable address: do not
            # route to it. Its next verification records the diagnostic.
            logger.warning(
                "not routing application %s: origin %s %s: %s",
                application.slug,
                origin.url,
                exc.code,
                exc.message,
            )
            continue
        groups.append(
            RouteGroup(
                application_slug=application.slug,
                origin=dial,
                origin_host=origin_host,
                origin_tls=origin.scheme == "https",
                hostnames=hostnames,
            )
        )
    return groups


def assertion_subrequest(settings: EdgeSettings) -> dict[str, Any]:
    """Forward-auth style subrequest: the API signs the assertion for this request.

    A 2xx answer copies the assertion header onto the request and continues to
    the origin; anything else is returned to the client as-is (403), so no
    request reaches an origin without an assertion.
    """
    request_headers = {
        EDGE_HOST_HEADER: ["{http.request.host}"],
        EDGE_SNI_HEADER: ["{http.request.tls.server_name}"],
        EDGE_REQUEST_ID_HEADER: ["{http.request.uuid}"],
        "X-Forwarded-Method": ["{http.request.method}"],
        "X-Forwarded-Uri": ["{http.request.uri}"],
    }
    if settings.edge_token:
        request_headers[EDGE_TOKEN_HEADER] = [settings.edge_token]
    return {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": settings.assert_upstream}],
        "rewrite": {"method": "GET", "uri": ASSERT_PATH},
        "headers": {"request": {"set": request_headers}},
        "handle_response": [
            {
                "match": {"status_code": [2]},
                "routes": [
                    {
                        "handle": [
                            {
                                "handler": "headers",
                                "request": {
                                    "set": {
                                        ASSERTION_HEADER: [
                                            "{http.reverse_proxy.header." + ASSERTION_HEADER + "}"
                                        ]
                                    }
                                },
                            }
                        ]
                    }
                ],
            }
        ],
    }


def _origin_proxy(group: RouteGroup) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": group.origin}],
        # The origin sees the customer-facing host; X-Forwarded-* say how it arrived.
        "headers": {
            "request": {
                "set": {
                    "Host": ["{http.request.host}"],
                    "X-Forwarded-Host": ["{http.request.host}"],
                    "X-Forwarded-Proto": ["{http.request.scheme}"],
                }
            }
        },
    }
    if group.origin_tls:
        # Verified TLS to the origin by its name, while dialing the pinned address.
        handler["transport"] = {"protocol": "http", "tls": {"server_name": group.origin_host}}
    return handler


def _route(group: RouteGroup, settings: EdgeSettings) -> dict[str, Any]:
    return {
        "@id": f"app-{group.application_slug}",
        "match": [{"host": list(group.hostnames)}],
        "handle": [
            {"handler": "headers", "request": {"delete": list(STRIPPED_REQUEST_HEADERS)}},
            assertion_subrequest(settings),
            _origin_proxy(group),
        ],
        "terminal": True,
    }


def health_route() -> dict[str, Any]:
    """Answers the readiness probe on every hostname so it can tell this edge apart."""
    return {
        "@id": "edge-health",
        "match": [{"path": [HEALTH_PATH]}],
        "handle": [
            {
                "handler": "static_response",
                "status_code": 204,
                "headers": {EDGE_HEALTH_HEADER: [EDGE_HEALTH_VALUE]},
            }
        ],
        "terminal": True,
    }


def unmatched_route() -> dict[str, Any]:
    """Explicit refusal for hostnames no application route matched (no catch-all proxy)."""
    return {
        "@id": "edge-unmatched",
        "handle": [{"handler": "static_response", "status_code": 404}],
        "terminal": True,
    }


def _server(settings: EdgeSettings, routes: list[dict[str, Any]]) -> dict[str, Any]:
    server: dict[str, Any] = {
        "listen": [f":{settings.https_port}"],
        "routes": [health_route(), *routes, unmatched_route()],
    }
    if settings.disable_https:
        server["automatic_https"] = {"disable": True}
    else:
        # Serve TLS even while no application route lists a hostname yet:
        # Caddy otherwise treats a server without host matchers as plain HTTP,
        # and on-demand issuance could never start for the first domain.
        server["tls_connection_policies"] = [{}]
        # A request whose Host differs from the TLS SNI is refused (421), so
        # the certificate, the route and the assertion always name one host.
        server["strict_sni_host"] = True
    return server


def build_apps(session: Session, settings: EdgeSettings) -> dict[str, Any]:
    """The ``apps`` subtree the reconciler manages: routing and TLS automation."""
    groups = serveable_route_groups(session)
    apps: dict[str, Any] = {"http": http_app(settings, [_route(g, settings) for g in groups])}
    if not settings.disable_https:
        # Certificates are issued on demand, at the first TLS handshake for a
        # hostname, and only when the ask endpoint approves that hostname.
        apps["tls"] = {
            "automation": {
                "on_demand": {"permission": {"module": "http", "endpoint": settings.ask_url}},
                "policies": [{"on_demand": True, "issuers": [settings.tls_issuer_config()]}],
            }
        }
    return apps


def http_app(settings: EdgeSettings, routes: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``http`` app: the edge server plus the ports Caddy uses for automatic HTTPS."""
    http: dict[str, Any] = {"servers": {SERVER_NAME: _server(settings, routes)}}
    if settings.http_port != 80:
        http["http_port"] = settings.http_port
    if settings.https_port != 443:
        http["https_port"] = settings.https_port
    return http


def admin_listen(settings: EdgeSettings) -> str:
    parsed = urlparse(settings.admin_url)
    return f"{parsed.hostname or 'localhost'}:{parsed.port or 2019}"


def build_bootstrap(settings: EdgeSettings) -> dict[str, Any]:
    """The configuration Caddy starts with: admin listener, certificate storage
    and an empty server. It holds the storage credentials, so it is written to a
    file only the caddy user can read (entrypoint.sh) and never sent through the
    admin API by the application."""
    config: dict[str, Any] = {
        "admin": {"listen": admin_listen(settings)},
        "apps": {"http": http_app(settings, [])},
    }
    storage = settings.storage_config()
    if storage is not None:
        config["storage"] = storage
    return config


def build_caddy_config(session: Session, settings: EdgeSettings) -> dict[str, Any]:
    """The complete desired configuration (bootstrap plus derived apps)."""
    config = build_bootstrap(settings)
    config["apps"] = build_apps(session, settings)
    return config


def app_route_count(apps: dict[str, Any]) -> int:
    """Number of application routes (excluding the fixed health route)."""
    routes = apps.get("http", {}).get("servers", {}).get(SERVER_NAME, {}).get("routes", [])
    return sum(1 for route in routes if str(route.get("@id", "")).startswith("app-"))


def config_digest(config: dict[str, Any] | None) -> str:
    if config is None:
        return "none"
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def hostnames_in(config: dict[str, Any] | None) -> set[str]:
    if not config:
        return set()
    hosts: set[str] = set()
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    for server in servers.values():
        for route in server.get("routes", []):
            for match in route.get("match", []):
                hosts.update(match.get("host", []))
    return hosts


def redact_apps_summary(apps: dict[str, Any]) -> dict[str, Any]:
    """Counts for display: application routes and hostnames, no secrets."""
    return {"routes": app_route_count(apps), "hostnames": len(hostnames_in({"apps": apps}))}
