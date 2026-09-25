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
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.edge.settings import HEALTH_PATH, EdgeSettings
from app.models import Application, ApplicationStatus, Domain, DomainStatus, VerifiedOrigin
from app.services.domains import is_serveable

SERVER_NAME = "edge"
EDGE_HEALTH_HEADER = "X-Custom-Domain-Edge"
EDGE_HEALTH_VALUE = "1"


@dataclass(frozen=True)
class RouteGroup:
    application_slug: str
    origin: str  # host:port
    origin_tls: bool
    hostnames: tuple[str, ...]


def serveable_route_groups(session: Session) -> list[RouteGroup]:
    """Hostnames to route, grouped per application, in a deterministic order."""
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
                Domain.status == DomainStatus.READY,
            )
            .options(
                joinedload(Domain.application),
                selectinload(Domain.claims),
                selectinload(Domain.checks),
            )
            .order_by(Domain.hostname)
        ).all()
        hostnames = tuple(d.hostname for d in domains if is_serveable(d))
        if not hostnames:
            continue
        groups.append(
            RouteGroup(
                application_slug=application.slug,
                origin=f"{origin.host}:{origin.port}",
                origin_tls=origin.scheme == "https",
                hostnames=hostnames,
            )
        )
    return groups


def _route(group: RouteGroup) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": group.origin}],
    }
    if group.origin_tls:
        handler["transport"] = {"protocol": "http", "tls": {}}
    return {
        "@id": f"app-{group.application_slug}",
        "match": [{"host": list(group.hostnames)}],
        "handle": [handler],
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


def _server(settings: EdgeSettings, routes: list[dict[str, Any]]) -> dict[str, Any]:
    server: dict[str, Any] = {
        "listen": [f":{settings.https_port}"],
        "routes": [health_route(), *routes],
    }
    if settings.disable_https:
        server["automatic_https"] = {"disable": True}
    return server


def build_apps(session: Session, settings: EdgeSettings) -> dict[str, Any]:
    """The ``apps`` subtree the reconciler manages: routing and TLS automation."""
    groups = serveable_route_groups(session)
    apps: dict[str, Any] = {
        "http": {"servers": {SERVER_NAME: _server(settings, [_route(g) for g in groups])}}
    }
    if not settings.disable_https:
        issuer: dict[str, Any] = {"module": "acme"}
        if settings.acme_email:
            issuer["email"] = settings.acme_email
        # Certificates are issued on demand, at the first TLS handshake for a
        # hostname, and only when the ask endpoint approves that hostname.
        apps["tls"] = {
            "automation": {
                "on_demand": {"permission": {"module": "http", "endpoint": settings.ask_url}},
                "policies": [{"on_demand": True, "issuers": [issuer]}],
            }
        }
    return apps


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
        "apps": {"http": {"servers": {SERVER_NAME: _server(settings, [])}}},
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
