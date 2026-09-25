"""Verify and parse webhook deliveries (docs/webhooks.md)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterable
from datetime import datetime

from custom_domain.models import Domain, WebhookEvent

HEADER = "X-Custom-Domain-Signature"
DEFAULT_TOLERANCE = 300


class SignatureInvalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _digest(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()


def verify_webhook(
    header: str | None,
    body: bytes,
    secrets: Iterable[str],
    *,
    now: int | None = None,
    tolerance: int = DEFAULT_TOLERANCE,
) -> int:
    """Return the delivery timestamp or raise :class:`SignatureInvalid`.

    Pass every secret you currently hold (the new and the previous one during
    a rotation).
    """
    if not header:
        raise SignatureInvalid("missing", "No signature header")
    timestamp: int | None = None
    given: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureInvalid("malformed", "Timestamp is not an integer") from exc
        elif key == "v1":
            given.append(value)
    if timestamp is None or not given:
        raise SignatureInvalid("malformed", "Signature header lacks t= or v1=")
    current = int(now if now is not None else time.time())
    if abs(current - timestamp) > tolerance:
        raise SignatureInvalid("stale", "Delivery timestamp outside tolerance")
    for secret in secrets:
        expected = _digest(secret, timestamp, body)
        if any(hmac.compare_digest(expected, candidate) for candidate in given):
            return timestamp
    raise SignatureInvalid("bad_signature", "No signature matched")


def parse_event(body: bytes) -> WebhookEvent:
    data = json.loads(body)
    return WebhookEvent(
        id=data["id"],
        type=data["type"],
        created_at=datetime.fromisoformat(data["created_at"]),
        domain=Domain.from_dict(data["data"]["domain"]),
        raw=data,
    )
