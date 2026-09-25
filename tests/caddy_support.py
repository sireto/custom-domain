"""Helpers for tests that run the real Caddy binary.

Locally these tests skip when ``caddy`` is not installed; in CI
(``REQUIRE_CADDY=1``) a missing binary fails the run instead of silently
skipping the edge contract.
"""

from __future__ import annotations

import os
import shutil
import socket
import time

import pytest


def caddy_required():
    """Marker: skip without Caddy locally, fail without it when required."""
    if shutil.which("caddy"):
        return pytest.mark.skipif(False, reason="")
    if os.environ.get("REQUIRE_CADDY"):
        pytest.fail("REQUIRE_CADDY is set but no caddy binary is on PATH", pytrace=False)
    return pytest.mark.skip(reason="caddy binary not installed")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False
