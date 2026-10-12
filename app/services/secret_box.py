"""Keys derived from ``SERVICE_SECRET_KEY``: encryption at rest and signed values.

``SERVICE_SECRET_KEY`` is a random value of at least 32 characters in the
deployment's ``.env`` (the installer generates it). It is never stored in
the database, so a dump or a backup alone does not reveal what it protects.
Each use derives its own key with HKDF-SHA256 and a distinct label:

* ``encrypt`` and ``decrypt``: AES-256-GCM for secrets the service must send
  back out (the remote Domain Connect signer's credential);
* ``seal`` and ``unseal``: an HMAC-SHA256-protected, expiring value, readable
  but not forgeable (the Domain Connect return ``state``).

Changing ``SERVICE_SECRET_KEY`` makes what was encrypted with the old one
unreadable: set the signer again afterwards.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any

from app.services.errors import ServiceError

MIN_LENGTH = 32
_PREFIX = "v1:"


class SecretKeyMissing(ServiceError):
    code = "service_secret_key_missing"


class Undecryptable(Exception):
    """The value was not encrypted with the current key, or was altered."""


def _secret(environ: Mapping[str, str] | None = None) -> bytes:
    env = os.environ if environ is None else environ
    value = env.get("SERVICE_SECRET_KEY", "").strip()
    if len(value) < MIN_LENGTH:
        raise SecretKeyMissing(
            f"SERVICE_SECRET_KEY must be set to a random value of at least {MIN_LENGTH} "
            "characters (the installer adds one); it protects secrets stored in the database"
        )
    return value.encode("utf-8")


def available(environ: Mapping[str, str] | None = None) -> bool:
    try:
        _secret(environ)
    except SecretKeyMissing:
        return False
    return True


def _derive(label: str, environ: Mapping[str, str] | None = None) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=f"custom-domain/{label}".encode()
    ).derive(_secret(environ))


def encrypt(plaintext: str, purpose: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_derive(f"encrypt/{purpose}")).encrypt(
        nonce, plaintext.encode("utf-8"), purpose.encode()
    )
    return _PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(ciphertext: str, purpose: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not ciphertext.startswith(_PREFIX):
        raise Undecryptable("unknown format")
    try:
        raw = base64.b64decode(ciphertext[len(_PREFIX) :], validate=True)
        plain = AESGCM(_derive(f"encrypt/{purpose}")).decrypt(raw[:12], raw[12:], purpose.encode())
    except (InvalidTag, ValueError, binascii.Error):
        raise Undecryptable("not encrypted with the current SERVICE_SECRET_KEY") from None
    return plain.decode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(payload: dict[str, Any], purpose: str) -> str:
    """``<payload>.<mac>``, both base64url: readable, but not forgeable without the key."""
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    mac = hmac.new(_derive(f"seal/{purpose}"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unseal(value: str, purpose: str) -> dict[str, Any] | None:
    """The payload of a value ``seal`` made, or None if it was not made with this key."""
    body, sep, mac = value.partition(".")
    if not sep or len(value) > 4096:
        return None
    try:
        expected = hmac.new(
            _derive(f"seal/{purpose}"), body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        data = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        return None
    return data if isinstance(data, dict) else None
