"""Signing Domain Connect apply URLs (docs/domain-connect.md).

An apply URL asks the customer's DNS provider to apply this service's
template to their zone. Providers accept it only when it is signed: the
signature is RS256 (RSASSA-PKCS1-v1_5 with SHA-256) over the query string
exactly as it appears in the URL, without ``key`` and ``sig``; ``key`` names
the TXT host under the template's ``syncPubKeyDomain`` where the public key
is published. ``sig`` goes last, because some providers require it.

Two signers produce the signature:

* ``LocalSigner`` holds the private key in this process, for a deployment
  that publishes its own template and key;
* ``RemoteSigner`` asks an external signing service, which keeps the key and
  checks each request against its own rules (the contract is in
  docs/domain-connect.md).

Nothing here logs or returns a key, a credential, a query (it carries the
challenge token and the caller's state) or a signature.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

DEFAULT_KEY_HOST = "_dcpubkeyv1"
REMOTE_TIMEOUT = 15.0
MIN_KEY_BITS = 2048
MAX_REMOTE_ANSWER = 16 * 1024

# The template's identifiers, as Domain Connect uses them in URL paths.
IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
# A TXT host label such as _dcpubkeyv1 (optionally several labels).
KEY_HOST = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,62})$")
# The remote signer's credential: dcs_ and 43 URL-safe base64 characters.
CREDENTIAL = re.compile(r"^dcs_[A-Za-z0-9_-]{43}$")
CREDENTIAL_PREFIX_LENGTH = 12
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# Refusals the remote signer documents, by HTTP status.
REMOTE_REFUSALS = frozenset(
    {
        "bad_request",
        "bad_query",
        "unauthorized",
        "foreign_template",
        "redirect_not_https",
        "wrong_target",
        "unknown_domain",
        "host_mismatch",
        "domain_not_live",
        "wrong_token",
    }
)


class InvalidSigningKey(ValueError):
    """The configured private key is unusable. The message never contains the key."""


class SigningFailed(Exception):
    """A signature could not be obtained.

    ``kind`` is ``refused`` (the signer will not sign this request),
    ``rate_limited`` (try again after ``retry_after`` seconds) or
    ``unavailable`` (the signer could not be reached or answered badly).
    """

    def __init__(
        self, kind: str, code: str, message: str, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class Signature:
    key: str
    sig: str = field(repr=False)


# --- the URL -------------------------------------------------------------------------


def canonical_query(params: Sequence[tuple[str, str]]) -> str:
    """The query string to sign: every value percent-encoded as RFC 3986 does it.

    Letters, digits and ``-._~`` stay as they are, everything else is
    ``%XX``, spaces included (never ``+``), so the signed string has exactly
    one spelling.
    """
    return urlencode(list(params), quote_via=quote)


def apply_url(
    url_sync_ux: str, provider_id: str, service_id: str, query: str, signature: Signature
) -> str:
    """The apply URL, with ``key`` and then ``sig`` last."""
    return (
        f"{url_sync_ux.rstrip('/')}/v2/domainTemplates/providers/{quote(provider_id, safe='')}"
        f"/services/{quote(service_id, safe='')}/apply?{query}"
        f"&key={quote(signature.key, safe='')}&sig={quote(signature.sig, safe='')}"
    )


def https_url_problem(value: str) -> str | None:
    """Why ``value`` is not an https URL with a host, no user part and no fragment."""
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in value):
        return "must not contain spaces or control characters"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "is not a valid URL"
    if parts.scheme != "https":
        return "must be an https URL"
    if not parts.hostname:
        return "must have a host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "must not contain a user name or password"
    if parts.fragment or "#" in value:
        return "must not contain a fragment"
    return None


# --- local keys ----------------------------------------------------------------------


@dataclass(frozen=True)
class LocalKey:
    host: str
    private_key: Any = field(repr=False)


def _load_key(value: str, position: int) -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import load_der_private_key

    try:
        der = base64.b64decode(value.strip(), validate=True)
        key = load_der_private_key(der, password=None)
    except (ValueError, TypeError, binascii.Error):
        raise InvalidSigningKey(
            f"Domain Connect key {position} is not the base64 of a PKCS#8 DER private key"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidSigningKey(f"Domain Connect key {position} is not an RSA key")
    if key.key_size < MIN_KEY_BITS:
        raise InvalidSigningKey(
            f"Domain Connect key {position} has {key.key_size} bits; at least "
            f"{MIN_KEY_BITS} are required"
        )
    return key


def parse_private_keys(value: str, key_host: str | None = None) -> tuple[LocalKey, ...]:
    """Keys from ``DOMAIN_CONNECT_PRIVATE_KEY``; the first one signs.

    Either one line holding the base64 of the PKCS#8 DER of an RSA key (its
    TXT host is ``key_host``, ``_dcpubkeyv1`` by default), or, during a
    rotation, ``<key host>:<base64>`` entries separated by commas, newest
    first, with ``key_host`` unset.
    """
    entries = [item.strip() for item in value.split(",") if item.strip()]
    if not entries:
        raise InvalidSigningKey("DOMAIN_CONNECT_PRIVATE_KEY is empty")
    if len(entries) == 1 and ":" not in entries[0]:
        host = key_host or DEFAULT_KEY_HOST
        if not KEY_HOST.match(host):
            raise InvalidSigningKey("DOMAIN_CONNECT_KEY_HOST is not a valid TXT host label")
        return (LocalKey(host, _load_key(entries[0], 1)),)
    if key_host:
        raise InvalidSigningKey(
            "With several keys (<key host>:<base64>,...), leave DOMAIN_CONNECT_KEY_HOST unset"
        )
    keys = []
    for position, entry in enumerate(entries, start=1):
        host, sep, encoded = entry.partition(":")
        if not sep or not KEY_HOST.match(host.strip()):
            raise InvalidSigningKey(
                f"Domain Connect key {position} must be written <key host>:<base64>"
            )
        keys.append(LocalKey(host.strip(), _load_key(encoded, position)))
    if len({key.host for key in keys}) != len(keys):
        raise InvalidSigningKey("Each Domain Connect key needs its own key host")
    return tuple(keys)


def public_key_records(private_key: Any) -> list[str]:
    """The TXT record values that publish the key's public half (Domain Connect format).

    ``p=1,a=RS256,d=<first 200 characters>``, ``p=2,...`` and so on, over the
    base64 of the SubjectPublicKeyInfo DER.
    """
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    der = private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    data = base64.b64encode(der).decode("ascii")
    chunks = [data[i : i + 200] for i in range(0, len(data), 200)]
    return [f"p={n},a=RS256,d={chunk}" for n, chunk in enumerate(chunks, start=1)]


class LocalSigner:
    """Signs with a key held by this process."""

    kind = "local"

    def __init__(self, keys: Sequence[LocalKey]) -> None:
        if not keys:
            raise InvalidSigningKey("No Domain Connect key configured")
        self.keys = tuple(keys)

    def __repr__(self) -> str:
        return f"<LocalSigner key_host={self.keys[0].host}>"

    def sign(self, query: str, *, application: str, domain_id: str) -> Signature:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        key = self.keys[0]
        raw = key.private_key.sign(query.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return Signature(key=key.host, sig=base64.b64encode(raw).decode("ascii"))


# --- the remote signer ---------------------------------------------------------------


class RemoteSigner:
    """Asks a signing service: ``POST {url}`` with ``Authorization: Bearer <credential>``.

    The body is ``{provider_id, service_id, application, domain_id, query}``
    and a ``200`` answer is ``{"sig", "key"}``. The service may call this
    deployment's v1 API back while it answers, hence the long timeout.
    Redirects are not followed.
    """

    kind = "remote"

    def __init__(
        self,
        url: str,
        credential: str,
        *,
        provider_id: str,
        service_id: str,
        timeout: float = REMOTE_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self._credential = credential
        self.provider_id = provider_id
        self.service_id = service_id
        self.timeout = timeout
        self.transport = transport

    def __repr__(self) -> str:
        return f"<RemoteSigner url={self.url}>"

    def sign(self, query: str, *, application: str, domain_id: str) -> Signature:
        body = {
            "provider_id": self.provider_id,
            "service_id": self.service_id,
            "application": application,
            "domain_id": domain_id,
            "query": query,
        }
        try:
            with httpx.Client(
                timeout=self.timeout, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.post(
                    self.url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._credential}",
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException:
            raise SigningFailed(
                "unavailable", "signer_timeout", "The signing service did not answer in time"
            ) from None
        except httpx.HTTPError:
            raise SigningFailed(
                "unavailable", "signer_unreachable", "The signing service could not be reached"
            ) from None
        return _read_answer(response)


def _error_code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and len(code) <= 64 else None


def _retry_after(response: httpx.Response) -> int:
    raw = response.headers.get("Retry-After", "").strip()
    return max(1, min(int(raw), 3600)) if raw.isdigit() else 60


def _read_answer(response: httpx.Response) -> Signature:
    status = response.status_code
    if len(response.content) > MAX_REMOTE_ANSWER:
        raise SigningFailed("unavailable", "signer_bad_answer", "The signer's answer is too large")
    if status == 200:
        try:
            data = response.json()
        except ValueError:
            data = None
        sig = data.get("sig") if isinstance(data, dict) else None
        key = data.get("key") if isinstance(data, dict) else None
        if (
            not isinstance(sig, str)
            or not isinstance(key, str)
            or not _BASE64.match(sig)
            or len(sig) > 2048
            or not KEY_HOST.match(key)
        ):
            raise SigningFailed(
                "unavailable", "signer_bad_answer", "The signing service's answer is not usable"
            )
        return Signature(key=key, sig=sig)
    code = _error_code(response)
    if status == 429:
        raise SigningFailed(
            "rate_limited",
            "rate_limited",
            "The signing service is rate limiting this deployment",
            retry_after=_retry_after(response),
        )
    if status in (400, 401, 403) and code in REMOTE_REFUSALS:
        return _refused(code)
    if status in (400, 401, 403):
        raise SigningFailed("refused", "signer_refused", "The signing service refused the request")
    if code == "verification_unavailable":
        raise SigningFailed(
            "unavailable",
            "verification_unavailable",
            "The signing service could not check the domain; try again shortly",
        )
    raise SigningFailed(
        "unavailable", "signer_error", f"The signing service answered HTTP {status}"
    )


def _refused(code: str) -> Signature:
    raise SigningFailed("refused", code, f"The signing service refused the request ({code})")
