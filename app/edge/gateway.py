"""Validating configuration gateway in front of Caddy's admin API.

Runs inside the edge container next to Caddy (docs/deployment.md). The API
and worker containers reach only this gateway, never Caddy's admin API. It
exposes exactly:

* ``GET /config/`` and ``GET /config/apps``: the running ``apps`` subtree
  (never ``admin`` or ``storage``);
* ``POST /config/apps``: replace the ``apps`` subtree, after validating that
  the payload has the shape the reconciler produces and nothing else.

Everything else is 404, including ``/load``. Validation is structural and
strict: the only handlers allowed are the header strip, the assert
subrequest to the configured upstream, ``reverse_proxy`` to an upstream that
the management API currently lists as a verified active origin, and the two
fixed body-less ``static_response`` routes. No ``file_server``, no other
listeners, no changes to TLS automation beyond the expected policy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from app.edge import config as edge_config
from app.edge.settings import EdgeSettings

logger = logging.getLogger(__name__)


class ConfigRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EdgeFacts:
    """What the management API says the edge may route to.

    ``upstreams`` maps each dialable ``address:port`` of a verified active
    origin to that origin's name; ``edge_names`` are the CNAME targets the
    portal may be served on (``GET /internal/edge/origins``).
    """

    upstreams: Mapping[str, str]
    edge_names: frozenset[str] = frozenset()
    # The portal allowlist as the API states it; the edge container needs no
    # copy of PORTAL_ALLOWED_IPS, so changing it never requires an edge restart.
    portal_ranges: tuple[str, ...] = ()


def validate_apps(apps: Any, settings: EdgeSettings, facts: Mapping[str, str] | EdgeFacts) -> None:
    """Raise ``ConfigRejected`` unless ``apps`` is exactly a reconciler-shaped subtree."""
    if not isinstance(facts, EdgeFacts):
        facts = EdgeFacts(upstreams=facts)
    allowed_upstreams = facts.upstreams
    if not isinstance(apps, dict):
        raise ConfigRejected("apps must be an object")
    if set(apps) - {"http", "tls"}:
        raise ConfigRejected(f"unexpected apps: {sorted(set(apps) - {'http', 'tls'})}")
    http = apps.get("http")
    expected_http = edge_config.http_app(settings, [])
    if not isinstance(http, dict) or set(http) != set(expected_http):
        raise ConfigRejected(f"http keys must be exactly {sorted(expected_http)}")
    for key, value in expected_http.items():
        if key != "servers" and http.get(key) != value:
            raise ConfigRejected(f"http.{key} must be {value!r}")
    servers = http["servers"]
    if not isinstance(servers, dict) or set(servers) != {edge_config.SERVER_NAME}:
        raise ConfigRejected(f"exactly one server named {edge_config.SERVER_NAME!r} is allowed")
    server = servers[edge_config.SERVER_NAME]
    expected_server = edge_config._server(settings, [])
    allowed_keys = set(expected_server) | {"routes"}
    if not isinstance(server, dict) or set(server) - allowed_keys:
        raise ConfigRejected(f"server keys must be a subset of {sorted(allowed_keys)}")
    for key, value in expected_server.items():
        if key != "routes" and server.get(key) != value:
            raise ConfigRejected(f"server.{key} must be {value!r}")
    routes = server.get("routes")
    if not isinstance(routes, list) or len(routes) < 2:
        raise ConfigRejected("routes must be a list starting with the health route")
    if routes[0] != edge_config.health_route():
        raise ConfigRejected("first route must be the health route")
    if routes[-1] != edge_config.unmatched_route():
        raise ConfigRejected("last route must be the unmatched route")
    middle = list(routes[1:-1])
    if (
        middle
        and isinstance(middle[0], dict)
        and str(middle[0].get("@id", "")).startswith("portal")
    ):
        # The portal pair must be exactly what the reconciler emits for edge
        # names the API vouches for, and only when addresses are allowed.
        if not facts.portal_ranges:
            raise ConfigRejected("portal routes are not allowed: the API exposes no portal")
        match = middle[0].get("match")
        hosts = (
            match[0].get("host")
            if isinstance(match, list) and match and isinstance(match[0], dict)
            else None
        )
        if not isinstance(hosts, list) or not hosts or set(hosts) - set(facts.edge_names):
            raise ConfigRejected("portal routes may only be served on the applications' edge names")
        expected = edge_config.portal_routes_for(
            sorted(hosts), list(facts.portal_ranges), settings.assert_upstream
        )
        if middle[:2] != expected:
            raise ConfigRejected("portal routes must be exactly the reconciler's portal routes")
        middle = middle[2:]
    seen_hosts: set[str] = set()
    for route in middle:
        _validate_app_route(route, settings, allowed_upstreams, seen_hosts)

    tls = apps.get("tls")
    if settings.disable_https:
        if tls is not None:
            raise ConfigRejected("tls is not allowed while HTTPS is disabled")
        return
    if tls != _expected_tls(settings):
        raise ConfigRejected("tls automation must be exactly the on-demand policy")


def _expected_tls(settings: EdgeSettings) -> dict[str, Any]:
    return {
        "automation": {
            "on_demand": {"permission": {"module": "http", "endpoint": settings.ask_url}},
            "policies": [{"on_demand": True, "issuers": [settings.tls_issuer_config()]}],
        }
    }


def _validate_app_route(
    route: Any, settings: EdgeSettings, allowed_upstreams: Mapping[str, str], seen_hosts: set[str]
) -> None:
    if not isinstance(route, dict) or set(route) != {"@id", "match", "handle", "terminal"}:
        raise ConfigRejected("application routes must have @id, match, handle and terminal only")
    if not str(route["@id"]).startswith("app-") or route["terminal"] is not True:
        raise ConfigRejected("application routes must be terminal and named app-<slug>")
    match = route["match"]
    if not (isinstance(match, list) and len(match) == 1 and set(match[0]) == {"host"}):
        raise ConfigRejected("application routes must match on host only")
    hosts = match[0]["host"]
    if not isinstance(hosts, list) or not hosts:
        raise ConfigRejected("host matcher must be a non-empty list")
    from app.hostname import InvalidHostname, canonicalize

    for host in hosts:
        try:
            canonical = canonicalize(str(host))
        except InvalidHostname as exc:
            raise ConfigRejected(
                f"host {host!r} is not a valid customer hostname: {exc.code}"
            ) from exc
        if canonical != host or canonical in seen_hosts:
            raise ConfigRejected(f"host {host!r} is not canonical or is duplicated")
        seen_hosts.add(canonical)
    handle = route["handle"]
    if not (isinstance(handle, list) and len(handle) == 3):
        raise ConfigRejected("application routes must have exactly three handlers")
    strip, assert_step, proxy = handle
    if strip != {
        "handler": "headers",
        "request": {"delete": list(edge_config.STRIPPED_REQUEST_HEADERS)},
    }:
        raise ConfigRejected("first handler must strip the reserved request headers")
    if assert_step != edge_config.assertion_subrequest(settings):
        raise ConfigRejected(
            "second handler must be the assert subrequest to the configured upstream"
        )
    if not isinstance(proxy, dict) or proxy.get("handler") != "reverse_proxy":
        raise ConfigRejected("third handler must be reverse_proxy")
    allowed_proxy_keys = {"handler", "upstreams", "headers", "transport"}
    if set(proxy) - allowed_proxy_keys:
        raise ConfigRejected(f"reverse_proxy keys must be a subset of {sorted(allowed_proxy_keys)}")
    upstreams = proxy.get("upstreams")
    if not (isinstance(upstreams, list) and len(upstreams) == 1 and set(upstreams[0]) == {"dial"}):
        raise ConfigRejected("reverse_proxy must have exactly one upstream with dial only")
    dial = upstreams[0]["dial"]
    if dial not in allowed_upstreams:
        raise ConfigRejected(f"upstream {dial!r} is not a verified active origin")
    expected_headers = {
        "request": {
            "set": {
                "Host": ["{http.request.host}"],
                "X-Forwarded-Host": ["{http.request.host}"],
                "X-Forwarded-Proto": ["{http.request.scheme}"],
            }
        }
    }
    if proxy.get("headers") != expected_headers:
        raise ConfigRejected("reverse_proxy headers must set Host and X-Forwarded-* only")
    transport = proxy.get("transport")
    origin_host = allowed_upstreams[dial]
    expected_transport = {"protocol": "http", "tls": {"server_name": origin_host}}
    if transport is not None and transport != expected_transport:
        raise ConfigRejected(
            "reverse_proxy transport may only enable verified TLS to the origin's own name"
        )


OriginsProvider = Callable[[], Mapping[str, str] | EdgeFacts]


def api_origins_provider(api_url: str, token: str | None, timeout: float = 5.0) -> OriginsProvider:
    """Fetch the verified active origins and edge names from the management API."""

    def fetch() -> EdgeFacts:
        headers = {"X-Custom-Domain-Edge-Token": token} if token else {}
        response = httpx.get(
            f"{api_url.rstrip('/')}/internal/edge/origins", headers=headers, timeout=timeout
        )
        response.raise_for_status()
        body = response.json()
        return EdgeFacts(
            upstreams={item["dial"]: item["host"] for item in body["upstreams"]},
            edge_names=frozenset(body.get("edge_names", [])),
            portal_ranges=tuple(body.get("portal_ranges", [])),
        )

    return fetch


def create_gateway_app(
    settings: EdgeSettings,
    *,
    caddy_admin_url: str,
    origins_provider: OriginsProvider,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Caddy configuration gateway", docs_url=None, redoc_url=None, openapi_url=None
    )
    client = httpx.Client(
        base_url=caddy_admin_url.rstrip("/"), timeout=timeout, transport=transport
    )

    def running_apps() -> Any:
        response = client.get("/config/apps")
        if (
            response.status_code != 200
            or not response.content
            or response.content.strip() == b"null"
        ):
            return None
        return response.json()

    @app.get("/config/")
    def get_config() -> Response:
        apps = running_apps()
        return _json(200, {"apps": apps} if apps is not None else None)

    @app.get("/config/apps")
    def get_apps() -> Response:
        return _json(200, running_apps())

    @app.post("/config/apps")
    async def set_apps(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return _json(400, {"error": "body must be JSON"})
        try:
            allowed = origins_provider()
        except Exception as exc:  # cannot verify upstreams: refuse
            logger.error("gateway could not fetch verified origins: %s", exc)
            return _json(503, {"error": "verified origins unavailable"})
        try:
            validate_apps(payload, settings, allowed)
        except ConfigRejected as exc:
            logger.warning("gateway rejected configuration: %s", exc.reason)
            return _json(400, {"error": exc.reason})
        upstream = client.post("/config/apps", json=payload)
        return Response(
            status_code=upstream.status_code,
            content=upstream.content,
            media_type="application/json",
        )

    return app


def _json(status: int, body: Any) -> Response:
    import json

    return Response(status_code=status, content=json.dumps(body), media_type="application/json")
