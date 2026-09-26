"""Operator sign-in for the portal.

One shared operator password (``PORTAL_PASSWORD``) opens a session carried
in a signed cookie; every state-changing form carries a CSRF token bound to
that session; failed sign-ins are rate limited per client address. The
portal is disabled, and answers 503, until a password of at least
``MIN_PASSWORD_LENGTH`` characters is configured. There is no user store:
a self-hosted deployment has one operator role, and the password is set by
whoever installs the service (``deploy/install.sh`` generates one).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.responses import Response

logger = logging.getLogger(__name__)

COOKIE = "cd_portal"
SESSION_TTL = 12 * 3600
MIN_PASSWORD_LENGTH = 12
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60


@dataclass(frozen=True)
class PortalSettings:
    password: str | None
    secret: bytes
    enabled: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PortalSettings:
        env = os.environ if environ is None else environ
        password = env.get("PORTAL_PASSWORD", "").strip() or None
        enabled = password is not None and len(password) >= MIN_PASSWORD_LENGTH
        if password is not None and not enabled:
            logger.error(
                "PORTAL_PASSWORD is shorter than %s characters; the portal stays disabled",
                MIN_PASSWORD_LENGTH,
            )
        secret_text = env.get("PORTAL_SESSION_SECRET", "").strip()
        if secret_text:
            secret = hashlib.sha256(secret_text.encode("utf-8")).digest()
        elif enabled:
            # Derived from the password so sessions survive a restart. Anyone
            # who knows the password can sign in anyway, so this adds no
            # exposure; set PORTAL_SESSION_SECRET to decouple the two.
            secret = hashlib.sha256(
                b"custom-domain-portal-session:" + password.encode("utf-8")  # type: ignore[union-attr]
            ).digest()
        else:
            secret = secrets.token_bytes(32)
        return cls(password=password if enabled else None, secret=secret, enabled=enabled)

    def check_password(self, given: str) -> bool:
        if not self.enabled or self.password is None:
            return False
        return hmac.compare_digest(given.encode("utf-8"), self.password.encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Sessions:
    """Signed, expiring session cookies. The cookie holds no secret of its own."""

    def __init__(self, settings: PortalSettings, ttl: int = SESSION_TTL) -> None:
        self.settings = settings
        self.ttl = ttl

    def _sign(self, payload: str) -> str:
        return _b64(
            hmac.new(self.settings.secret, payload.encode("ascii"), hashlib.sha256).digest()
        )

    def issue(self, now: float | None = None) -> tuple[str, str]:
        """Return ``(cookie value, csrf token)`` for a fresh session."""
        stamp = int(now if now is not None else time.time())
        csrf = secrets.token_urlsafe(32)
        payload = _b64(
            json.dumps(
                {"v": 1, "exp": stamp + self.ttl, "csrf": csrf}, separators=(",", ":")
            ).encode()
        )
        return f"{payload}.{self._sign(payload)}", csrf

    def read(self, cookie: str | None, now: float | None = None) -> dict | None:
        if not cookie or "." not in cookie:
            return None
        payload, _, signature = cookie.rpartition(".")
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, UnicodeDecodeError):
            return None
        stamp = int(now if now is not None else time.time())
        if not isinstance(data, dict) or data.get("v") != 1 or int(data.get("exp", 0)) < stamp:
            return None
        if not isinstance(data.get("csrf"), str):
            return None
        return data

    def set_cookie(self, response: Response, value: str, *, secure: bool) -> None:
        response.set_cookie(
            COOKIE,
            value,
            max_age=self.ttl,
            path="/portal",
            httponly=True,
            samesite="strict",
            secure=secure,
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE, path="/portal")


class LoginLimiter:
    """At most ``MAX_FAILURES`` failed sign-ins per client per ``FAILURE_WINDOW``."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: int = FAILURE_WINDOW) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        stamps = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = stamps
        return stamps

    def allowed(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            return len(self._recent(key, now)) < self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._recent(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def safe_next(path: str | None) -> str:
    """Only portal paths may be used as a post-login destination."""
    if path and path.startswith("/portal") and not path.startswith("//"):
        return path
    return "/portal"
