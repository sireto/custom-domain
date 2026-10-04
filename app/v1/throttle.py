"""Refuse repeated failed authentication from one client before touching the database.

With ``PUBLIC_API`` the v1 API is reachable from the internet. A request with
a wrong credential costs a hash and a database lookup; after too many of
them from one client in a minute, further requests from that client are
answered 429 before either.

Only addresses that identify a client are limited (``limited_client``): the
real client the trusted edge forwarded, or a public address connecting
directly. A private or loopback peer is never limited, because behind an
operator's own reverse proxy (the ``PUBLIC_API=false`` setup) every
application arrives from the proxy's one address, and one caller with a
stale key must not lock every application out.

IPv6 clients are counted per /64 (one subscriber usually holds a whole /64)
and, with a larger budget, per /48, so rotating through the /64s of a routed
/48 does not multiply the budget. The limiter lives in the API process's
memory; it caps how many buckets it tracks and evicts the one that failed
least recently in constant time, so address rotation cannot grow it or make
each request expensive. With several API processes each keeps its own count.
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time

from starlette.requests import Request

DEFAULT_FAILURES_PER_MINUTE = 30
WINDOW = 60.0
MAX_TRACKED = 50_000
# A /48 holds 65,536 /64s; it may fail this many times more than one /64.
PREFIX48_FACTOR = 10


def limited_client(request: Request) -> str | None:
    """The client address to count failures against, or None when it identifies no client."""
    from app.clients import client_address

    peer = request.client.host if request.client else None
    address = client_address(request)
    if address and address != peer:
        return address  # forwarded by the trusted edge: the real client
    try:
        return address if address and ipaddress.ip_address(address).is_global else None
    except ValueError:
        return None


def buckets(address: str) -> list[tuple[str, int]]:
    """The buckets a client address counts against, with their budget multiplier."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return [(address, 1)]
    if ip.version == 6:
        return [
            (str(ipaddress.ip_network(f"{ip}/64", strict=False)), 1),
            (str(ipaddress.ip_network(f"{ip}/48", strict=False)), PREFIX48_FACTOR),
        ]
    return [(str(ip), 1)]


class FailedAuthLimiter:
    def __init__(
        self,
        max_failures: int = DEFAULT_FAILURES_PER_MINUTE,
        window: float = WINDOW,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.max_tracked = max_tracked
        # Insertion-ordered: a bucket moves to the end each time it fails, so
        # the first entry is always the one that failed least recently.
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> FailedAuthLimiter:
        raw = os.environ.get("V1_AUTH_FAILURES_PER_MINUTE", "").strip()
        return cls(max_failures=int(raw) if raw else DEFAULT_FAILURES_PER_MINUTE)

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def retry_after(self, address: str, now: float | None = None) -> int:
        """Seconds until ``address`` may try again; 0 when it may now."""
        if not self.enabled:
            return 0
        now = time.monotonic() if now is None else now
        wait = 0
        with self._lock:
            for key, factor in buckets(address):
                stamps = [t for t in self._failures.get(key, ()) if now - t < self.window]
                if len(stamps) >= self.max_failures * factor:
                    wait = max(wait, int(stamps[0] + self.window - now) + 1)
        return wait

    def record_failure(self, address: str, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            for key, factor in buckets(address):
                limit = self.max_failures * factor
                stamps = [t for t in self._failures.pop(key, ()) if now - t < self.window]
                stamps.append(now)
                self._failures[key] = stamps[-limit:]  # older stamps cannot matter
                while len(self._failures) > self.max_tracked:
                    del self._failures[next(iter(self._failures))]

    def tracked(self) -> int:
        with self._lock:
            return len(self._failures)


class CredentialRateLimiter:
    """At most ``per_minute`` v1 requests per credential in any 60 seconds.

    Off unless ``V1_REQUESTS_PER_MINUTE`` is set, for deployments that serve
    several applications and must keep one from overloading the API. Counted
    per credential (one application's keys never spend another's budget), in
    the API process's memory: with several API processes each keeps its own
    count. Each credential keeps at most ``per_minute`` timestamps, and there
    are no more credentials than the database holds.
    """

    def __init__(self, per_minute: int = 0, window: float = WINDOW) -> None:
        self.per_minute = per_minute
        self.window = window
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> CredentialRateLimiter:
        raw = os.environ.get("V1_REQUESTS_PER_MINUTE", "").strip()
        if not raw:
            return cls()
        if not raw.isdigit():
            raise ValueError(f"V1_REQUESTS_PER_MINUTE must be a whole number, not {raw!r}")
        return cls(per_minute=int(raw))

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def take(self, credential_id: str, now: float | None = None) -> int:
        """Count one request; return 0 if it may go ahead, else seconds to wait."""
        if not self.enabled:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            stamps = [t for t in self._seen.get(credential_id, ()) if now - t < self.window]
            if len(stamps) >= self.per_minute:
                self._seen[credential_id] = stamps
                return int(stamps[0] + self.window - now) + 1
            stamps.append(now)
            self._seen[credential_id] = stamps
            return 0
