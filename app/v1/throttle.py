"""Refuse repeated failed authentication from one client before touching the database.

With ``PUBLIC_API`` the v1 API is reachable from the internet. A request with
a wrong credential costs a hash and a database lookup; after too many of
them from one client address in a minute, further requests from that
address are answered 429 before either. The client is the real address
behind the edge (``app.clients``). IPv6 clients are counted per /64,
because one subscriber usually holds a whole /64.

The limiter lives in the API process's memory. It prunes old entries and
caps how many addresses it tracks, so address rotation cannot grow it
without bound; with several API processes each keeps its own count.
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time

DEFAULT_FAILURES_PER_MINUTE = 30
WINDOW = 60.0
MAX_TRACKED = 50_000


def client_key(address: str | None) -> str:
    if not address:
        return "unknown"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


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
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> FailedAuthLimiter:
        raw = os.environ.get("V1_AUTH_FAILURES_PER_MINUTE", "").strip()
        return cls(max_failures=int(raw) if raw else DEFAULT_FAILURES_PER_MINUTE)

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until ``key`` may try again; 0 when it may now."""
        if not self.enabled:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            stamps = self._recent(key, now)
            if len(stamps) < self.max_failures:
                return 0
            return max(1, int(stamps[0] + self.window - now) + 1)

    def record_failure(self, key: str, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            if key not in self._failures and len(self._failures) >= self.max_tracked:
                self._prune(now)
                if len(self._failures) >= self.max_tracked:
                    # Still full of active entries: drop the oldest-started one.
                    oldest = min(self._failures, key=lambda k: self._failures[k][0])
                    del self._failures[oldest]
            self._recent(key, now, create=True).append(now)

    def _recent(self, key: str, now: float, *, create: bool = False) -> list[float]:
        """The failures of ``key`` inside the window; only recording creates an entry."""
        stamps = [t for t in self._failures.get(key, ()) if now - t < self.window]
        if stamps or create:
            self._failures[key] = stamps
        else:
            self._failures.pop(key, None)
        return stamps

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._failures.items() if not v or now - v[-1] >= self.window]:
            del self._failures[key]

    def tracked(self) -> int:
        with self._lock:
            return len(self._failures)
