"""Resolve the workspace from the edge assertion in your web framework.

:class:`WorkspaceResolver` is framework neutral: give it the request headers
and host and it returns the verified :class:`Assertion` or raises.
:class:`CustomDomainMiddleware` wraps an ASGI application, stores the
assertion in ``scope["state"]["custom_domain"]``, serves the workspace probe
the lifecycle worker uses (``/.well-known/custom-domain-workspace``), and by
default rejects requests that carry no valid assertion on the custom-domain
path (``on_missing="reject"``). Use ``on_missing="passthrough"`` when the
same application also serves its own domain and decides per request.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from custom_domain.assertion import HEADER, Assertion, AssertionInvalid, verify_assertion

WORKSPACE_PATH = "/.well-known/custom-domain-workspace"


class WorkspaceResolver:
    def __init__(
        self,
        keys: Mapping[str, str | bytes],
        application_id: str,
        *,
        skew: int = 30,
    ) -> None:
        self.keys = dict(keys)
        self.application_id = application_id
        self.skew = skew

    def resolve(
        self, headers: Mapping[str, str] | Iterable[tuple[str, str]], host: str | None
    ) -> Assertion:
        """Verify the assertion for this request. Raises :class:`AssertionInvalid`."""
        lookup = {
            k.lower(): v for k, v in (headers.items() if isinstance(headers, Mapping) else headers)
        }
        token = lookup.get(HEADER.lower())
        hostname = host.split(":", 1)[0] if host else None
        return verify_assertion(
            token,
            self.keys,
            expected_application_id=self.application_id,
            expected_hostname=hostname,
            skew=self.skew,
        )


ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]


class CustomDomainMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        keys: Mapping[str, str | bytes],
        application_id: str,
        workspace_lookup: Callable[[str], Any],
        on_missing: str = "reject",
        state_key: str = "custom_domain",
    ) -> None:
        """``workspace_lookup(reference)`` must return a truthy value only when the
        application actually serves that workspace; it may be sync or async. The
        workspace probe answers 200 only for a confirmed workspace, so readiness
        proves real tenant selection rather than an echo of the reference."""
        if on_missing not in ("reject", "passthrough"):
            raise ValueError("on_missing must be 'reject' or 'passthrough'")
        if not callable(workspace_lookup):
            raise ValueError("workspace_lookup must be a callable taking the workspace reference")
        self.app = app
        self.resolver = WorkspaceResolver(keys, application_id)
        self.workspace_lookup = workspace_lookup
        self.on_missing = on_missing
        self.state_key = state_key

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
        host = next((v for k, v in headers if k.lower() == "host"), None)
        try:
            assertion: Assertion | None = self.resolver.resolve(headers, host)
        except AssertionInvalid as exc:
            if exc.code == "missing" and self.on_missing == "passthrough":
                assertion = None
            else:
                await _json(send, 403, {"error": "assertion_" + exc.code, "message": exc.message})
                return
        scope.setdefault("state", {})[self.state_key] = assertion
        if scope.get("path") == WORKSPACE_PATH:
            if assertion is None:
                await _json(send, 403, {"error": "assertion_missing"})
                return
            found = self.workspace_lookup(assertion.reference)
            if inspect.isawaitable(found):
                found = await found
            if not found:
                await _json(send, 404, {"error": "workspace_not_found"})
                return
            await _json(
                send,
                200,
                {"reference": assertion.reference, "application": assertion.application_id},
            )
            return
        await self.app(scope, receive, send)


async def _json(send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
