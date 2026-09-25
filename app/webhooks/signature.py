"""Delivery signatures.

``X-Custom-Domain-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>]`` where each
``v1`` is HMAC-SHA256 over ``<t>.<raw body>`` with one of the subscription's
secrets (the current one and, during rotation, the previous one). Consumers
verify with any secret they hold and reject timestamps outside the tolerance.
This module is what the SDK (#11) ships to consumers.
"""

from __future__ import annotations

import hashlib
import hmac
import time

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


def sign(body: bytes, secrets: list[str], *, timestamp: int | None = None) -> str:
    stamp = int(timestamp if timestamp is not None else time.time())
    parts = [f"t={stamp}"] + [f"v1={_digest(secret, stamp, body)}" for secret in secrets]
    return ",".join(parts)


def verify(
    header: str | None,
    body: bytes,
    secrets: list[str],
    *,
    now: int | None = None,
    tolerance: int = DEFAULT_TOLERANCE,
) -> int:
    """Return the delivery timestamp or raise ``SignatureInvalid``."""
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
