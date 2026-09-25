"""Legacy single-application domain API. Deprecated.

Kept for existing deployments during the migration to ``/v1`` (docs/api-v1.md).
Disable with ``ENABLE_LEGACY_API=false``. Requests still write the Caddy
configuration directly and accept an arbitrary ``upstream``; neither exists
in v1.
"""

from fastapi import APIRouter, Depends, Response
from fastapi.openapi.models import APIKey

from app.security import get_api_key

DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Link": '</v1/docs>; rel="successor-version"',
}

domain_api = APIRouter(tags=["Legacy Custom Domain API"], deprecated=True)


def _caddy():
    # Imported lazily: constructing the Caddy client contacts the admin API.
    from app.caddy.caddy import caddy_server

    return caddy_server


def _deprecated(response: Response) -> None:
    response.headers.update(DEPRECATION_HEADERS)


@domain_api.get("/domains", summary="[Deprecated] List hostnames in the Caddy config")
async def get_domains(response: Response, api_key: APIKey = Depends(get_api_key)):
    _deprecated(response)
    return _caddy().list_domains()


@domain_api.post("/domains", summary="[Deprecated] Add a hostname to the Caddy config")
async def add_domain(
    domain: str,
    response: Response,
    upstream: str | None = None,
    api_key: APIKey = Depends(get_api_key),
):
    _deprecated(response)
    _caddy().add_custom_domain(domain, upstream)
    return "OK"


@domain_api.delete("/domains", summary="[Deprecated] Remove a hostname from the Caddy config")
async def remove_domains(domain: str, response: Response, api_key: APIKey = Depends(get_api_key)):
    _deprecated(response)
    _caddy().remove_custom_domain(domain)
    return "OK"
