"""Endpoints for the edge itself. Not part of the public contract.

* ``GET /internal/tls/ask?domain=<hostname>`` is Caddy's on-demand TLS
  permission check: 200 allows issuance or renewal, 403 denies. It is one
  indexed lookup and never touches DNS.
* ``GET /internal/edge/assert`` is the per-request routing decision: Caddy
  calls it before proxying, with the request's Host, SNI and id in headers.
  200 returns the signed workspace assertion for the origin; 403 stops the
  request at the edge. Nothing is proxied without a 200 here.

Only trusted client addresses (the loopback by default,
``EDGE_ASK_TRUSTED_HOSTS``) may call either.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request, Response, status

from app.edge.assertion import HEADER as ASSERTION_HEADER
from app.edge.assertion import sign
from app.edge.config import EDGE_HOST_HEADER, EDGE_REQUEST_ID_HEADER, EDGE_SNI_HEADER, routable
from app.edge.settings import WORKSPACE_PATH
from app.hostname import InvalidHostname, canonicalize
from app.services.domains import find_live_by_hostname, is_serveable
from app.services.edge_checks import certificate_authorized
from app.v1.deps import DbSession

router = APIRouter(prefix="/internal", include_in_schema=False, tags=["internal"])


def _settings(request: Request):
    return getattr(request.app.state, "edge_settings", None)


def _trusted(request: Request) -> bool:
    settings = _settings(request)
    trusted = settings.ask_trusted_hosts if settings else ("127.0.0.1", "::1")
    client = request.client.host if request.client else None
    return client is not None and client in trusted


@router.get("/tls/ask")
def tls_ask(
    request: Request, db: DbSession, domain: str = Query(min_length=1, max_length=253)
) -> Response:
    if not _trusted(request):
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        row = find_live_by_hostname(db, domain)
    except InvalidHostname:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if certificate_authorized(row):
        return Response(status_code=status.HTTP_200_OK)
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.get("/edge/assert")
def edge_assert(
    request: Request,
    db: DbSession,
    host: str | None = Header(default=None, alias=EDGE_HOST_HEADER),
    sni: str | None = Header(default=None, alias=EDGE_SNI_HEADER),
    request_id: str | None = Header(default=None, alias=EDGE_REQUEST_ID_HEADER),
    forwarded_uri: str | None = Header(default=None, alias="X-Forwarded-Uri"),
) -> Response:
    if not _trusted(request):
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    settings = _settings(request)
    if settings is None or not settings.assertion_keys:
        # Fail closed: without a signing key nothing is routed.
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if not host:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        hostname = canonicalize(host.rsplit(":", 1)[0] if host.count(":") == 1 else host)
    except InvalidHostname:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    # Caddy leaves the SNI placeholder unreplaced on plain-HTTP requests.
    if sni and not sni.startswith("{"):
        try:
            if canonicalize(sni) != hostname:
                return Response(status_code=status.HTTP_403_FORBIDDEN)
        except InvalidHostname:
            return Response(status_code=status.HTTP_403_FORBIDDEN)

    domain = find_live_by_hostname(db, hostname)
    if domain is None:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if not is_serveable(domain):
        # Before readiness only the workspace probe may pass, so the
        # lifecycle worker can prove tenant selection through the edge.
        path = (forwarded_uri or "").split("?", 1)[0]
        if not (routable(domain) and path == WORKSPACE_PATH):
            return Response(status_code=status.HTTP_403_FORBIDDEN)
    origin = domain.application.serving_origin
    if origin is None:
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
    return Response(status_code=status.HTTP_200_OK, headers={ASSERTION_HEADER: token})
