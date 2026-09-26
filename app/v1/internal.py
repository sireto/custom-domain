"""Endpoints for the edge itself. Not part of the public contract.

* ``GET /internal/tls/ask?domain=<hostname>``: Caddy's on-demand TLS
  permission check (200 allow, 403 deny); one indexed lookup, no DNS.
* ``GET /internal/edge/assert``: per-request routing decision; 200 returns
  the signed workspace assertion, 403 stops the request at the edge.
* ``GET /internal/edge/origins``: verified active origins, for the
  configuration gateway to validate upstreams.
* ``GET /internal/metrics``: Prometheus exposition.

Callers must come from ``EDGE_ASK_TRUSTED_HOSTS`` (addresses or CIDRs). When
``EDGE_TOKEN`` is set, the assert, origins and metrics endpoints also
require it in ``X-Custom-Domain-Edge-Token``; the ask endpoint cannot carry
headers and relies on the address check alone.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, Query, Request, Response, status
from sqlalchemy import select

from app import observability
from app.edge.assertion import HEADER as ASSERTION_HEADER
from app.edge.assertion import sign
from app.edge.config import (
    EDGE_HOST_HEADER,
    EDGE_REQUEST_ID_HEADER,
    EDGE_SNI_HEADER,
    EDGE_TOKEN_HEADER,
    routable,
)
from app.edge.settings import WORKSPACE_PATH
from app.hostname import InvalidHostname, canonicalize
from app.models import Application, ApplicationStatus, OriginStatus, VerifiedOrigin
from app.services import origin_verification
from app.services.domains import find_live_by_hostname, is_serveable
from app.services.edge_checks import certificate_authorized, is_edge_name
from app.services.origin_verification import OriginVerificationFailed, allow_private_from_env
from app.v1.deps import DbSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", include_in_schema=False, tags=["internal"])


def _settings(request: Request):
    return getattr(request.app.state, "edge_settings", None)


def _trusted(request: Request) -> bool:
    settings = _settings(request)
    client = request.client.host if request.client else None
    if settings is None:
        return client in ("127.0.0.1", "::1")
    return settings.trusts(client)


def _token_ok(request: Request, token: str | None) -> bool:
    settings = _settings(request)
    expected = settings.edge_token if settings else None
    if not expected:
        return True
    return bool(token) and hmac.compare_digest(token, expected)


@router.get("/tls/ask")
def tls_ask(
    request: Request, db: DbSession, domain: str = Query(min_length=1, max_length=253)
) -> Response:
    if not _trusted(request):
        observability.tls_ask_total.labels(decision="untrusted").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        row = find_live_by_hostname(db, domain)
    except InvalidHostname:
        observability.tls_ask_total.labels(decision="denied").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if certificate_authorized(row) or is_edge_name(db, domain):
        observability.tls_ask_total.labels(decision="allowed").inc()
        return Response(status_code=status.HTTP_200_OK)
    observability.tls_ask_total.labels(decision="denied").inc()
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.get("/edge/assert")
def edge_assert(
    request: Request,
    db: DbSession,
    host: str | None = Header(default=None, alias=EDGE_HOST_HEADER),
    sni: str | None = Header(default=None, alias=EDGE_SNI_HEADER),
    request_id: str | None = Header(default=None, alias=EDGE_REQUEST_ID_HEADER),
    edge_token: str | None = Header(default=None, alias=EDGE_TOKEN_HEADER),
    forwarded_uri: str | None = Header(default=None, alias="X-Forwarded-Uri"),
) -> Response:
    if not _trusted(request) or not _token_ok(request, edge_token):
        observability.edge_assert_total.labels(decision="untrusted").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    settings = _settings(request)
    if settings is None or not settings.assertion_keys:
        # Fail closed: without a signing key nothing is routed.
        observability.edge_assert_total.labels(decision="unavailable").inc()
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if not host:
        observability.edge_assert_total.labels(decision="denied").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        hostname = canonicalize(host.rsplit(":", 1)[0] if host.count(":") == 1 else host)
    except InvalidHostname:
        observability.edge_assert_total.labels(decision="denied").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    # Caddy leaves the SNI placeholder unreplaced on plain-HTTP requests.
    if sni and not sni.startswith("{"):
        try:
            if canonicalize(sni) != hostname:
                observability.edge_assert_total.labels(decision="denied").inc()
                return Response(status_code=status.HTTP_403_FORBIDDEN)
        except InvalidHostname:
            observability.edge_assert_total.labels(decision="denied").inc()
            return Response(status_code=status.HTTP_403_FORBIDDEN)

    domain = find_live_by_hostname(db, hostname)
    if domain is None:
        observability.edge_assert_total.labels(decision="denied").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if not is_serveable(domain):
        # Before readiness only the workspace probe may pass, so the
        # lifecycle worker can prove tenant selection through the edge.
        path = (forwarded_uri or "").split("?", 1)[0]
        if not (routable(domain) and path == WORKSPACE_PATH):
            observability.edge_assert_total.labels(decision="denied").inc()
            return Response(status_code=status.HTTP_403_FORBIDDEN)
    origin = domain.application.serving_origin
    if origin is None:
        observability.edge_assert_total.labels(decision="denied").inc()
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    key_id, key = settings.active_key()
    token = sign(
        key_id=key_id,
        key=key,
        application_id=str(domain.application_id),
        domain_id=str(domain.id),
        reference=domain.reference,
        hostname=hostname,
        request_id=(request_id or "")[:64] or "unknown",
        ttl=settings.assertion_ttl,
    )
    observability.edge_assert_total.labels(decision="allowed").inc()
    return Response(status_code=status.HTTP_200_OK, headers={ASSERTION_HEADER: token})


@router.get("/edge/origins")
def edge_origins(
    request: Request,
    db: DbSession,
    edge_token: str | None = Header(default=None, alias=EDGE_TOKEN_HEADER),
) -> dict:
    if not _trusted(request) or not _token_ok(request, edge_token):
        return Response(status_code=status.HTTP_403_FORBIDDEN)  # type: ignore[return-value]
    rows = db.execute(
        select(VerifiedOrigin.host, VerifiedOrigin.port)
        .join(Application, Application.id == VerifiedOrigin.application_id)
        .where(
            VerifiedOrigin.is_active.is_(True),
            VerifiedOrigin.status == OriginStatus.VERIFIED,
            Application.status == ApplicationStatus.ACTIVE,
        )
    ).all()
    # The edge dials a pinned address and presents the origin's name, so the
    # gateway needs both: every address the name resolves to right now, paired
    # with the name. An origin that no longer resolves acceptably is left out,
    # which makes the gateway refuse a configuration that still routes to it.
    upstreams = []
    for host, port in rows:
        try:
            dials = origin_verification.resolve_dials(
                host, port, allow_private=allow_private_from_env()
            )
        except OriginVerificationFailed as exc:
            logger.warning("origin %s:%s not offered to the edge: %s", host, port, exc.message)
            continue
        upstreams.extend({"dial": dial, "host": host} for dial in dials)
    upstreams.sort(key=lambda u: (u["host"], u["dial"]))
    return {"upstreams": upstreams}


@router.get("/metrics")
def metrics(
    request: Request,
    db: DbSession,
    edge_token: str | None = Header(default=None, alias=EDGE_TOKEN_HEADER),
) -> Response:
    if not _trusted(request) or not _token_ok(request, edge_token):
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    return Response(
        content=observability.render_metrics(db), media_type="text/plain; version=0.0.4"
    )
