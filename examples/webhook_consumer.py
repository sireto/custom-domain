"""Sample webhook consumer: verifies signatures, deduplicates, tolerates reordering.

Run: WEBHOOK_SECRET=whsec_... python examples/webhook_consumer.py
Then subscribe the URL http://<host>:8080/hooks (https in production).

The interesting part is ``Consumer.handle``: it is safe to call with the same
delivery twice and with deliveries arriving out of order, which the delivery
worker's retries and the replay endpoint can both cause.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.webhooks.signature import HEADER, SignatureInvalid, verify  # noqa: E402


class Consumer:
    def __init__(self, secrets: list[str]) -> None:
        self.secrets = secrets
        self.seen: set[str] = set()  # event ids already applied
        self.latest: dict[str, datetime] = {}  # domain id -> created_at of newest applied event
        self.state: dict[str, dict] = {}  # domain id -> last known domain resource

    def handle(self, body: bytes, signature: str | None, *, now: int | None = None) -> str:
        """Return what happened: 'applied', 'duplicate', 'stale' or raise SignatureInvalid."""
        verify(signature, body, self.secrets, now=now)
        event = json.loads(body)
        event_id = event["id"]
        if event_id in self.seen:
            return "duplicate"
        domain = event["data"]["domain"]
        created = datetime.fromisoformat(event["created_at"])
        self.seen.add(event_id)
        newest = self.latest.get(domain["id"])
        if newest is not None and created < newest:
            # An older event delivered late: remember it, but do not move state backwards.
            return "stale"
        self.latest[domain["id"]] = created
        self.state[domain["id"]] = domain
        return "applied"


def serve(consumer: Consumer, port: int = 8080) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                outcome = consumer.handle(body, self.headers.get(HEADER))
            except SignatureInvalid as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(exc.code.encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(outcome.encode())

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    secrets = [s for s in os.environ.get("WEBHOOK_SECRET", "").split(",") if s]
    if not secrets:
        sys.exit("set WEBHOOK_SECRET")
    serve(Consumer(secrets))
