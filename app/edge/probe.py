"""Readiness probe: is this hostname served over valid HTTPS by this edge?

Connects to the edge with the hostname as SNI, verifies the certificate chain
and hostname, then requests the edge health path and checks the response
carries this service's marker header. That proves, in one step, that a
certificate exists and is valid, that the handshake works for the hostname,
and that the address the probe reached is one of our edges rather than a
stale server the customer still points at.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime

from app.edge.config import EDGE_HEALTH_HEADER
from app.edge.settings import HEALTH_PATH, WORKSPACE_PATH

USER_AGENT = "custom-domain-readiness-probe/1"
X509_HOSTNAME_MISMATCH = 62
X509_CERT_EXPIRED = 10


class EdgeProbeFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EdgeProbe:
    address: str
    not_after: datetime
    issuer: str
    status: int
    edge_header: str | None
    not_before: datetime | None = None


@dataclass(frozen=True)
class WorkspaceProbe:
    """What the origin answered on the workspace path, through the edge."""

    address: str
    status: int
    reference: str | None
    application_id: str | None
    body: str


def _connect_tls(hostname: str, address: str, port: int, ca_file: str | None, timeout: float):
    try:
        sock = socket.create_connection((address, port), timeout=timeout)
    except TimeoutError as exc:
        raise EdgeProbeFailed(
            "timeout", f"Connecting to {address}:{port} for {hostname} timed out"
        ) from exc
    except OSError as exc:
        raise EdgeProbeFailed(
            "connection_failed", f"Cannot connect to {address}:{port} for {hostname}: {exc}"
        ) from exc
    try:
        context = ssl.create_default_context(cafile=ca_file)
        return context.wrap_socket(sock, server_hostname=hostname)
    except ssl.SSLCertVerificationError as exc:
        sock.close()
        code = "certificate_untrusted"
        if exc.verify_code == X509_HOSTNAME_MISMATCH:
            code = "certificate_hostname_mismatch"
        elif exc.verify_code == X509_CERT_EXPIRED:
            code = "certificate_expired"
        raise EdgeProbeFailed(
            code,
            f"The certificate served for {hostname} did not verify: {exc.verify_message or exc}",
        ) from exc
    except ssl.SSLError as exc:
        sock.close()
        raise EdgeProbeFailed(
            "tls_handshake_failed",
            f"TLS handshake for {hostname} failed: {exc}. On the first handshake the edge "
            "requests a certificate; if issuance was denied or failed, Caddy's log has "
            "the ACME error.",
        ) from exc
    except TimeoutError as exc:
        sock.close()
        raise EdgeProbeFailed(
            "timeout",
            f"TLS handshake for {hostname} timed out; the edge may still be obtaining the "
            "certificate. Retrying.",
        ) from exc


def probe_workspace(
    hostname: str,
    *,
    address: str,
    port: int = 443,
    ca_file: str | None = None,
    timeout: float = 15.0,
    workspace_path: str = WORKSPACE_PATH,
    plain_http: bool = False,
) -> WorkspaceProbe:
    """Request the workspace echo path through the edge and parse the origin's answer."""
    import json

    if plain_http:
        try:
            sock = socket.create_connection((address, port), timeout=timeout)
        except OSError as exc:
            raise EdgeProbeFailed(
                "connection_failed", f"Cannot connect to {address}:{port} for {hostname}: {exc}"
            ) from exc
    else:
        sock = _connect_tls(hostname, address, port, ca_file, timeout)
    try:
        connection = http.client.HTTPConnection(hostname, port, timeout=timeout)
        connection.sock = sock
        connection.request(
            "GET",
            workspace_path,
            headers={"Host": hostname, "User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        response = connection.getresponse()
        body = response.read(4096).decode("utf-8", "replace")
        reference = application_id = None
        if response.status == 200:
            try:
                data = json.loads(body)
                reference = str(data["reference"]) if "reference" in data else None
                application_id = str(data["application"]) if "application" in data else None
            except (ValueError, TypeError):
                pass
        return WorkspaceProbe(address, response.status, reference, application_id, body[:200])
    except TimeoutError as exc:
        raise EdgeProbeFailed(
            "timeout", f"{hostname} did not answer the workspace request"
        ) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise EdgeProbeFailed(
            "connection_failed", f"Workspace request to {hostname} failed: {exc}"
        ) from exc
    finally:
        sock.close()


def _issuer_name(cert: dict) -> str:
    parts = []
    for rdn in cert.get("issuer", ()):
        for key, value in rdn:
            if key in ("organizationName", "commonName"):
                parts.append(value)
    return ", ".join(parts) or "unknown"


def probe_edge(
    hostname: str,
    *,
    address: str,
    port: int = 443,
    ca_file: str | None = None,
    timeout: float = 15.0,
    health_path: str = HEALTH_PATH,
) -> EdgeProbe:
    try:
        sock = socket.create_connection((address, port), timeout=timeout)
    except TimeoutError as exc:
        raise EdgeProbeFailed(
            "timeout", f"Connecting to {address}:{port} for {hostname} timed out"
        ) from exc
    except OSError as exc:
        raise EdgeProbeFailed(
            "connection_failed", f"Cannot connect to {address}:{port} for {hostname}: {exc}"
        ) from exc
    try:
        context = ssl.create_default_context(cafile=ca_file)
        try:
            tls = context.wrap_socket(sock, server_hostname=hostname)
        except ssl.SSLCertVerificationError as exc:
            code = "certificate_untrusted"
            if exc.verify_code == X509_HOSTNAME_MISMATCH:
                code = "certificate_hostname_mismatch"
            elif exc.verify_code == X509_CERT_EXPIRED:
                code = "certificate_expired"
            raise EdgeProbeFailed(
                code,
                f"The certificate served for {hostname} did not verify: "
                f"{exc.verify_message or exc}",
            ) from exc
        except ssl.SSLError as exc:
            raise EdgeProbeFailed(
                "tls_handshake_failed",
                f"TLS handshake for {hostname} failed: {exc}. On the first handshake the edge "
                "requests a certificate; if issuance was denied or failed, Caddy's log has "
                "the ACME error.",
            ) from exc
        except TimeoutError as exc:
            raise EdgeProbeFailed(
                "timeout",
                f"TLS handshake for {hostname} timed out; the edge may still be obtaining the "
                "certificate. Retrying.",
            ) from exc

        cert = tls.getpeercert()
        not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), UTC)
        not_before = (
            datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notBefore"]), UTC)
            if cert.get("notBefore")
            else None
        )
        connection = http.client.HTTPConnection(hostname, port, timeout=timeout)
        connection.sock = tls
        connection.request("GET", health_path, headers={"Host": hostname, "User-Agent": USER_AGENT})
        response = connection.getresponse()
        response.read(1024)
        return EdgeProbe(
            address=address,
            not_after=not_after,
            issuer=_issuer_name(cert),
            status=response.status,
            edge_header=response.getheader(EDGE_HEALTH_HEADER),
            not_before=not_before,
        )
    except EdgeProbeFailed:
        raise
    except TimeoutError as exc:
        raise EdgeProbeFailed("timeout", f"{hostname} did not answer the health request") from exc
    except (http.client.HTTPException, OSError) as exc:
        raise EdgeProbeFailed(
            "connection_failed", f"Health request to {hostname} failed: {exc}"
        ) from exc
    finally:
        sock.close()
