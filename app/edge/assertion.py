"""Edge-signed workspace assertion.

Every request the edge proxies to an origin carries
``X-Custom-Domain-Assertion``: a compact token binding the request to the
application, domain, workspace reference and hostname the edge routed it for,
with an issue time, an expiry and the request id. Origins verify the
signature and the age, check the intended application, and only then trust
the workspace reference. See docs/edge-routing.md.

Format: ``v1.<key id>.<base64url payload>.<base64url HMAC-SHA256>`` where the
payload is compact JSON and the MAC covers ``v1.<key id>.<payload>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

HEADER = "X-Custom-Domain-Assertion"
VERSION = "v1"
DEFAULT_TTL = 60
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "app": self.application_id,
            "dom": self.domain_id,
            "ref": self.reference,
            "host": self.hostname,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "rid": self.request_id,
        }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _mac(key: bytes, signing_input: str) -> bytes:
    return hmac.new(key, signing_input.encode("utf-8"), hashlib.sha256).digest()


def parse_keys(spec: str) -> dict[str, bytes]:
    """``"2:secret-b,1:secret-a"`` to ``{"2": b"secret-b", "1": b"secret-a"}`` (first is active)."""
    keys: dict[str, bytes] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        key_id, sep, secret = item.partition(":")
        if not sep or not key_id.strip() or len(secret) < 32:
            raise ValueError(
                "EDGE_ASSERTION_KEYS entries must be <key id>:<secret of at least 32 characters>"
            )
        keys[key_id.strip()] = secret.encode("utf-8")
    return keys


def sign(
    *,
    key_id: str,
    key: bytes,
    application_id: str,
    domain_id: str,
    reference: str,
    hostname: str,
    request_id: str,
    now: int | None = None,
    ttl: int = DEFAULT_TTL,
) -> str:
    issued = int(now if now is not None else time.time())
    assertion = Assertion(
        application_id, domain_id, reference, hostname, issued, issued + ttl, request_id, key_id
    )
    payload = _b64(
        json.dumps(assertion.to_payload(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{VERSION}.{key_id}.{payload}"
    return f"{signing_input}.{_b64(_mac(key, signing_input))}"


def verify(
    token: str | None,
    keys: dict[str, bytes],
    *,
    expected_application_id: str | None = None,
    expected_hostname: str | None = None,
    now: int | None = None,
    skew: int = DEFAULT_SKEW,
) -> Assertion:
    """Return the assertion or raise ``AssertionInvalid`` with a stable code."""
    if not token:
        raise AssertionInvalid("missing", "No assertion header")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise AssertionInvalid("malformed", "Assertion is not a v1 token")
    _version, key_id, payload, signature = parts
    key = keys.get(key_id)
    if key is None:
        raise AssertionInvalid("unknown_key", f"Assertion signed with unknown key id {key_id!r}")
    expected = _mac(key, f"{VERSION}.{key_id}.{payload}")
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
    if expected_application_id is not None and assertion.application_id != expected_application_id:
        raise AssertionInvalid("wrong_application", "Assertion is for another application")
    if expected_hostname is not None and assertion.hostname != expected_hostname:
        raise AssertionInvalid("wrong_hostname", "Assertion is for another hostname")
    return assertion
