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

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.edge.settings import EdgeSettings
from app.models import Application, ApplicationStatus, Domain, DomainStatus, VerifiedOrigin
from app.services.domains import is_serveable

SERVER_NAME = "edge"


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
        origin: VerifiedOrigin | None = application.active_origin
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


def build_caddy_config(session: Session, settings: EdgeSettings) -> dict[str, Any]:
    groups = serveable_route_groups(session)
    server: dict[str, Any] = {
        "listen": [f":{settings.https_port}"],
        "routes": [_route(group) for group in groups],
    }
    if settings.disable_https:
        server["automatic_https"] = {"disable": True}

    config: dict[str, Any] = {"apps": {"http": {"servers": {SERVER_NAME: server}}}}
    storage = settings.storage_config()
    if storage is not None:
        config["storage"] = storage
    if settings.acme_email and not settings.disable_https:
        config["apps"]["tls"] = {
            "automation": {
                "policies": [{"issuers": [{"module": "acme", "email": settings.acme_email}]}]
            }
        }
    return config


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
