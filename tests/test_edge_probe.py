import shutil
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.edge.config import EDGE_HEALTH_HEADER, EDGE_HEALTH_VALUE
from app.edge.probe import EdgeProbeFailed, probe_edge

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")

HOST = "forms.customer.example"


def _make_cert(tmp_path, name, *, cn=HOST, days=30):
    key = tmp_path / f"{name}.key"
    cert = tmp_path / f"{name}.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            str(days),
            "-subj",
            f"/CN={cn}",
            "-addext",
            f"subjectAltName=DNS:{cn}",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


class EdgeServer:
    def __init__(self, cert, key, *, status=204, marker=True):
        self.status = status
        self.marker = marker
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(server.status)
                if server.marker:
                    self.send_header(EDGE_HEALTH_HEADER, EDGE_HEALTH_VALUE)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def cert(tmp_path):
    return _make_cert(tmp_path, "edge")


def test_probe_validates_certificate_hostname_and_marker(cert):
    cert_file, key_file = cert
    server = EdgeServer(cert_file, key_file)
    try:
        probe = probe_edge(
            HOST, address="127.0.0.1", port=server.port, ca_file=cert_file, timeout=5
        )
        assert probe.status == 204 and probe.edge_header == EDGE_HEALTH_VALUE
        assert probe.address == "127.0.0.1"
        assert timedelta(days=29) < probe.not_after - datetime.now(UTC) <= timedelta(days=30)
        assert probe.issuer == HOST
    finally:
        server.close()


def test_probe_rejects_wrong_hostname_and_untrusted_chain(cert):
    cert_file, key_file = cert
    server = EdgeServer(cert_file, key_file)
    try:
        with pytest.raises(EdgeProbeFailed) as info:
            probe_edge(
                "other.customer.example",
                address="127.0.0.1",
                port=server.port,
                ca_file=cert_file,
                timeout=5,
            )
        assert info.value.code == "certificate_hostname_mismatch"
        with pytest.raises(EdgeProbeFailed) as info:
            probe_edge(HOST, address="127.0.0.1", port=server.port, timeout=5)  # system CAs only
        assert info.value.code == "certificate_untrusted"
    finally:
        server.close()


def test_probe_reports_foreign_server_and_transport_failures(cert):
    cert_file, key_file = cert
    server = EdgeServer(cert_file, key_file, status=200, marker=False)
    try:
        probe = probe_edge(
            HOST, address="127.0.0.1", port=server.port, ca_file=cert_file, timeout=5
        )
        assert probe.status == 200 and probe.edge_header is None
    finally:
        server.close()

    plain = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    try:
        with pytest.raises(EdgeProbeFailed) as info:
            probe_edge(
                HOST,
                address="127.0.0.1",
                port=plain.server_address[1],
                ca_file=cert_file,
                timeout=5,
            )
        assert info.value.code == "tls_handshake_failed"
        assert "first handshake" in info.value.message
    finally:
        plain.shutdown()
        plain.server_close()

    with pytest.raises(EdgeProbeFailed) as info:
        probe_edge(HOST, address="127.0.0.1", port=server.port, ca_file=cert_file, timeout=5)
    assert info.value.code == "connection_failed"
