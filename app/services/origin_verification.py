"""Prove that the SaaS operator controls an origin before traffic is sent to it.

The origin must serve its verification token as the plain-text body of
``GET /.well-known/custom-domain-origin-verification``. The probe is built to
be safe to run against operator-supplied hostnames:

* the hostname is resolved once and every address must be publicly routable
  (no loopback, private, link-local, metadata, multicast or reserved ranges)
  unless private origins are expressly allowed for a trusted self-hosted
  deployment;
* the connection is made to the resolved address, not by name, so a DNS
  answer cannot change between the check and the connection (rebinding);
* TLS is verified against the system trust store with the hostname as SNI;
* redirects are not followed, the body is capped, and timeouts are short.

Failures carry a stable ``code`` and a message an operator can act on.
"""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import VerifiedOrigin
from app.services.applications import record_origin_verification

WELL_KNOWN_PATH = "/.well-known/custom-domain-origin-verification"
DEFAULT_TIMEOUT = 5.0
MAX_BODY_BYTES = 4096
USER_AGENT = "custom-domain-origin-verifier/1"


class OriginVerificationFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Probe:
    address: str
    status: int
    body: str


def allow_private_from_env() -> bool:
    return os.environ.get("ORIGIN_ALLOW_PRIVATE", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Resolve ``host`` and refuse non-public addresses unless allowed."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OriginVerificationFailed(
            "dns_resolution_failed", f"{host} does not resolve: {exc.strerror or exc}"
        ) from exc
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise OriginVerificationFailed("dns_resolution_failed", f"{host} has no addresses")
    if not allow_private:
        blocked = [a for a in addresses if not ipaddress.ip_address(a).is_global]
        if blocked:
            raise OriginVerificationFailed(
                "private_address_blocked",
                f"{host} resolves to non-public address(es) {', '.join(blocked)}; origins must "
                "be publicly routable. Set ORIGIN_ALLOW_PRIVATE=true only for a trusted "
                "self-hosted deployment.",
            )
    return addresses


def fetch_token(
    host: str,
    port: int,
    scheme: str,
    *,
    allow_private: bool = False,
    path: str = WELL_KNOWN_PATH,
    timeout: float = DEFAULT_TIMEOUT,
) -> Probe:
    """GET the well-known path from the origin, pinned to a resolved address."""
    addresses = resolve(host, port, allow_private=allow_private)
    last_error: OriginVerificationFailed | None = None
    for address in addresses:
        try:
            return _probe(address, host, port, scheme, path, timeout)
        except OriginVerificationFailed as exc:
            if exc.code in ("connection_failed", "timeout"):
                last_error = exc
                continue
            raise
    assert last_error is not None
    raise last_error


def _probe(address: str, host: str, port: int, scheme: str, path: str, timeout: float) -> Probe:
    try:
        sock = socket.create_connection((address, port), timeout=timeout)
    except TimeoutError as exc:
        raise OriginVerificationFailed(
            "timeout", f"Connecting to {host} ({address}:{port}) timed out after {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise OriginVerificationFailed(
            "connection_failed", f"Cannot connect to {host} ({address}:{port}): {exc}"
        ) from exc

    try:
        if scheme == "https":
            context = ssl.create_default_context()
            try:
                sock = context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                raise OriginVerificationFailed(
                    "tls_verification_failed",
                    f"The certificate presented by {host} is not valid for it or not trusted: "
                    f"{exc.verify_message or exc}",
                ) from exc
            except ssl.SSLError as exc:
                raise OriginVerificationFailed(
                    "tls_handshake_failed",
                    f"TLS handshake with {host} ({address}:{port}) failed: {exc}. Is the origin "
                    "serving HTTPS on this port?",
                ) from exc
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.sock = sock  # already connected (and wrapped); no name-based connect
        default_port = 443 if scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        connection.request(
            "GET",
            path,
            headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "text/plain"},
        )
        response = connection.getresponse()
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise OriginVerificationFailed(
                "response_too_large",
                f"{path} returned more than {MAX_BODY_BYTES} bytes; serve only the token",
            )
        return Probe(address, response.status, body.decode("utf-8", "replace").strip())
    except OriginVerificationFailed:
        raise
    except TimeoutError as exc:
        raise OriginVerificationFailed(
            "timeout", f"{host} did not answer within {timeout:g}s"
        ) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise OriginVerificationFailed(
            "connection_failed", f"Request to {host} ({address}:{port}) failed: {exc}"
        ) from exc
    finally:
        sock.close()


def verify_origin(
    session: Session,
    origin: VerifiedOrigin,
    *,
    allow_private: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    now: datetime | None = None,
) -> VerifiedOrigin:
    """Run the proof-of-control probe and record the outcome on the origin.

    Raises ``OriginVerificationFailed`` after recording the failure, so the
    caller can report the diagnostic and the origin row shows it too.
    """
    expected = origin.verification_token or ""
    try:
        probe = fetch_token(
            origin.host, origin.port, origin.scheme, allow_private=allow_private, timeout=timeout
        )
        if probe.status != 200:
            raise OriginVerificationFailed(
                "unexpected_status",
                f"GET {WELL_KNOWN_PATH} on {origin.host} returned HTTP {probe.status}; expected "
                "200 with the verification token as the plain-text body",
            )
        if not expected or not hmac.compare_digest(probe.body, expected):
            raise OriginVerificationFailed(
                "token_mismatch",
                f"GET {WELL_KNOWN_PATH} on {origin.host} returned a body that is not this "
                "origin's verification token; check the token was copied exactly",
            )
    except OriginVerificationFailed as exc:
        record_origin_verification(
            session, origin, verified=False, error_code=exc.code, message=exc.message, now=now
        )
        raise
    record_origin_verification(session, origin, verified=True, now=now)
    return origin
