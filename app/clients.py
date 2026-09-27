"""Who a request came from, when the edge is in front.

Through the edge (a peer on ``EDGE_ASK_TRUSTED_HOSTS``) the connecting
address is the edge container's; the real client is the last
``X-Forwarded-For`` value, which Caddy sets to the peer it saw (it does not
pass on a client's own header). From any other peer that header is
ignored, so it cannot be spoofed.
"""

from __future__ import annotations

from starlette.requests import Request


def client_address(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    edge_settings = getattr(request.app.state, "edge_settings", None)
    forwarded = request.headers.get("x-forwarded-for", "")
    if peer and forwarded and edge_settings is not None and edge_settings.trusts(peer):
        return forwarded.split(",")[-1].strip() or peer
    return peer
