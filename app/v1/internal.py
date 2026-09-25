"""Endpoints for the edge itself. Not part of the public contract.

``GET /internal/tls/ask?domain=<hostname>`` is Caddy's on-demand TLS
permission check: 200 allows issuance or renewal, 403 denies. It is one
indexed lookup and never touches DNS. Only trusted client addresses (the
loopback by default, `EDGE_ASK_TRUSTED_HOSTS`) may call it.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status

from app.hostname import InvalidHostname
from app.services.domains import find_live_by_hostname
from app.services.edge_checks import certificate_authorized
from app.v1.deps import DbSession

router = APIRouter(prefix="/internal", include_in_schema=False, tags=["internal"])


def _trusted(request: Request) -> bool:
    settings = getattr(request.app.state, "edge_settings", None)
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
