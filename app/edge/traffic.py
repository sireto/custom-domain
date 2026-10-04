"""Per-hostname traffic totals from Caddy's metrics.

The edge enables ``apps.http.metrics.per_host`` (``app/edge/config.py``), so
Caddy's ``/metrics`` counts every request once per hostname it is configured
for, under the route's first handler, and the response body bytes with it.
Hostnames it is not configured for share the ``_other`` label. The gateway
passes on only these totals (``render_totals``); the reconciler reads them
with ``parse_totals`` whether it talks to the gateway or to Caddy directly.
"""

from __future__ import annotations

import re

from prometheus_client.parser import text_string_to_metric_families

from app.edge.config import SERVER_NAME

REQUESTS = "caddy_http_requests_total"
RESPONSE_BYTES = "caddy_http_response_size_bytes_sum"
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def parse_totals(text: str) -> dict[str, tuple[int, int]]:
    """Map each hostname on the edge server to its (requests, response bytes)."""
    totals: dict[str, list[int]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name not in (REQUESTS, RESPONSE_BYTES):
                continue
            if sample.labels.get("server") != SERVER_NAME:
                continue
            host = sample.labels.get("host", "").lower()
            if not _HOST.match(host):
                continue  # "_other", or no per-host label at all
            entry = totals.setdefault(host, [0, 0])
            entry[0 if sample.name == REQUESTS else 1] += int(sample.value)
    return {host: (requests, sent) for host, (requests, sent) in totals.items()}


def render_totals(totals: dict[str, tuple[int, int]]) -> str:
    """The totals as exposition text ``parse_totals`` reads back."""
    lines = []
    for host in sorted(totals):
        requests, sent = totals[host]
        labels = f'{{host="{host}",server="{SERVER_NAME}"}}'
        lines.append(f"{REQUESTS}{labels} {requests}")
        lines.append(f"{RESPONSE_BYTES}{labels} {sent}")
    return "\n".join(lines) + "\n"
