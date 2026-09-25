import socket
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.models import OriginStatus
from app.models.types import utcnow
from app.services.applications import (
    authenticate_credential,
    get_active_origin,
    issue_credential,
    register_origin,
    rotate_credential,
)
from app.services.errors import CredentialNotFound
from app.services.origin_verification import (
    WELL_KNOWN_PATH,
    OriginVerificationFailed,
    fetch_token,
    resolve,
    verify_origin,
)


class TokenServer:
    """Local HTTP origin serving a configurable body at the well-known path."""

    def __init__(self):
        self.body = b""
        self.status = 200
        self.path_seen = None
        self.host_seen = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                server.path_seen = self.path
                server.host_seen = self.headers.get("Host")
                self.send_response(server.status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def origin_server():
    server = TokenServer()
    yield server
    server.close()


@pytest.fixture
def origin(session, make_application, origin_server):
    acme = make_application("acme")
    origin = register_origin(
        session, acme, host="localhost", scheme="http", port=origin_server.port
    )
    session.commit()
    origin_server.body = origin.verification_token.encode()
    return origin


def test_resolve_blocks_non_public_addresses_unless_allowed():
    with pytest.raises(OriginVerificationFailed) as info:
        resolve("localhost", 80)
    assert info.value.code == "private_address_blocked"
    assert "ORIGIN_ALLOW_PRIVATE" in info.value.message
    assert resolve("localhost", 80, allow_private=True)
    with pytest.raises(OriginVerificationFailed) as info:
        resolve("origin.invalid", 443)
    assert info.value.code == "dns_resolution_failed"


def test_fetch_token_pins_address_and_sends_host_header(origin_server):
    origin_server.body = b"tok-123\n"
    probe = fetch_token("localhost", origin_server.port, "http", allow_private=True, timeout=3)
    assert probe.status == 200 and probe.body == "tok-123"
    assert probe.address == "127.0.0.1"
    assert origin_server.path_seen == WELL_KNOWN_PATH
    assert origin_server.host_seen == f"localhost:{origin_server.port}"


def test_verify_origin_success_records_verified(session, origin, origin_server):
    verify_origin(session, origin, allow_private=True, timeout=3)
    session.commit()
    assert origin.status == OriginStatus.VERIFIED
    assert origin.verified_at is not None and origin.last_error_code is None


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda s, o: setattr(s, "body", b"something-else"), "token_mismatch"),
        (lambda s, o: setattr(s, "status", 404), "unexpected_status"),
        (lambda s, o: setattr(s, "status", 302), "unexpected_status"),
        (lambda s, o: setattr(s, "body", b"x" * 5000), "response_too_large"),
    ],
)
def test_verify_origin_failures_are_recorded_with_codes(
    session, origin, origin_server, setup, code
):
    setup(origin_server, origin)
    with pytest.raises(OriginVerificationFailed) as info:
        verify_origin(session, origin, allow_private=True, timeout=3)
    session.commit()
    assert info.value.code == code
    assert origin.status == OriginStatus.FAILED
    assert origin.last_error_code == code
    assert origin.last_error_message and origin.is_active is False


def test_verify_origin_refuses_private_address_by_default(session, origin):
    with pytest.raises(OriginVerificationFailed) as info:
        verify_origin(session, origin, timeout=3)
    assert info.value.code == "private_address_blocked"
    assert origin.status == OriginStatus.FAILED


def test_https_against_a_plain_server_is_a_handshake_failure(
    session, make_application, origin_server
):
    acme = make_application("acme")
    origin = register_origin(
        session, acme, host="localhost", scheme="https", port=origin_server.port
    )
    with pytest.raises(OriginVerificationFailed) as info:
        verify_origin(session, origin, allow_private=True, timeout=3)
    assert info.value.code in ("tls_handshake_failed", "connection_failed")
    assert origin.last_error_code == info.value.code


def test_connection_failures_and_timeouts_have_codes(session, make_application):
    acme = make_application("acme")
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    free_port = closed.getsockname()[1]
    closed.close()
    origin = register_origin(session, acme, host="localhost", scheme="http", port=free_port)
    with pytest.raises(OriginVerificationFailed) as info:
        verify_origin(session, origin, allow_private=True, timeout=2)
    assert info.value.code == "connection_failed"

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        origin2 = register_origin(
            session, acme, host="localhost", scheme="http", port=silent.getsockname()[1]
        )
        with pytest.raises(OriginVerificationFailed) as info:
            verify_origin(session, origin2, allow_private=True, timeout=0.5)
        assert info.value.code == "timeout"
    finally:
        silent.close()


def test_rotate_credential_keeps_old_one_through_grace(session, make_application):
    acme = make_application("acme")
    old, old_secret = issue_credential(session, acme, label="backend")
    session.commit()
    t0 = utcnow()
    new, new_secret, old_again = rotate_credential(
        session, acme, old.id, grace=timedelta(hours=1), now=t0
    )
    session.commit()
    assert old_again.id == old.id and new.label == "backend"
    assert old.expires_at == t0 + timedelta(hours=1)
    assert authenticate_credential(session, old_secret, now=t0 + timedelta(minutes=30)).id == old.id
    assert authenticate_credential(session, new_secret).id == new.id
    from app.services.errors import InvalidCredential

    with pytest.raises(InvalidCredential):
        authenticate_credential(session, old_secret, now=t0 + timedelta(hours=2))

    # Rotating again does not extend an earlier expiry, and is application scoped.
    _, _, again = rotate_credential(session, acme, old.id, grace=timedelta(days=7), now=t0)
    assert again.expires_at == t0 + timedelta(hours=1)
    globex = make_application("globex")
    with pytest.raises(CredentialNotFound):
        rotate_credential(session, globex, old.id)


def test_active_origin_is_dropped_when_reverification_fails(session, origin, origin_server):
    from app.services.applications import activate_origin

    verify_origin(session, origin, allow_private=True, timeout=3)
    activate_origin(session, origin)
    session.commit()
    assert get_active_origin(session, origin.application).id == origin.id
    origin_server.body = b"rotated"
    with pytest.raises(OriginVerificationFailed):
        verify_origin(session, origin, allow_private=True, timeout=3)
    session.commit()
    assert get_active_origin(session, origin.application) is None
