"""Verify the edge-signed workspace assertion (docs/edge-routing.md).

Standalone copy of the service's algorithm so origins need only this package.
Format: ``v1.<key id>.<base64url payload>.<base64url HMAC-SHA256>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass

HEADER = "X-Custom-Domain-Assertion"
VERSION = "v1"
DEFAULT_SKEW = 30


class AssertionInvalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Assertion:
    application_id: str
    domain_id: str
    reference: str
    hostname: str
    issued_at: int
    expires_at: int
    request_id: str
    key_id: str


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_assertion(
    token: str | None,
    keys: Mapping[str, str | bytes],
    *,
    expected_application_id: str,
    expected_hostname: str | None = None,
    now: int | None = None,
    skew: int = DEFAULT_SKEW,
) -> Assertion:
    """Return the assertion or raise :class:`AssertionInvalid` with a stable code.

    ``keys`` maps key id to secret (accept the current and previous key during
    rotation). ``expected_application_id`` is your application's id as given
    by the operator; it is always checked.
    """
    if not token:
        raise AssertionInvalid("missing", "No assertion header")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise AssertionInvalid("malformed", "Assertion is not a v1 token")
    _, key_id, payload, signature = parts
    secret = keys.get(key_id)
    if secret is None:
        raise AssertionInvalid("unknown_key", f"Assertion signed with unknown key id {key_id!r}")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = hmac.new(key, f"{VERSION}.{key_id}.{payload}".encode(), hashlib.sha256).digest()
    try:
        given = _unb64(signature)
    except Exception as exc:
        raise AssertionInvalid("malformed", "Assertion signature is not base64url") from exc
    if not hmac.compare_digest(expected, given):
        raise AssertionInvalid("bad_signature", "Assertion signature does not verify")
    try:
        data = json.loads(_unb64(payload))
        assertion = Assertion(
            application_id=str(data["app"]),
            domain_id=str(data["dom"]),
            reference=str(data["ref"]),
            hostname=str(data["host"]),
            issued_at=int(data["iat"]),
            expires_at=int(data["exp"]),
            request_id=str(data["rid"]),
            key_id=key_id,
        )
    except Exception as exc:
        raise AssertionInvalid("malformed", "Assertion payload is not valid") from exc
    current = int(now if now is not None else time.time())
    if assertion.issued_at > current + skew:
        raise AssertionInvalid("not_yet_valid", "Assertion issued in the future")
    if assertion.expires_at + skew < current:
        raise AssertionInvalid("expired", "Assertion has expired")
    if assertion.application_id != expected_application_id:
        raise AssertionInvalid("wrong_application", "Assertion is for another application")
    if expected_hostname is not None and assertion.hostname != expected_hostname.lower().rstrip(
        "."
    ):
        raise AssertionInvalid("wrong_hostname", "Assertion is for another hostname")
    return assertion
