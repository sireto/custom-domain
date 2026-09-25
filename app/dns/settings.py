from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class DnsSettings:
    worker_enabled: bool = True
    worker_interval: float = 10.0
    batch_size: int = 50
    nameservers: tuple[str, ...] = ()
    timeout: float = 5.0
    # "public" queries DNS; "local" answers from the records the service issued
    # (development only, refused unless the edge is local as well).
    verification_mode: str = "public"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DnsSettings:
        env = os.environ if environ is None else environ
        enabled = env.get("DNS_WORKER_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        nameservers = tuple(
            item.strip() for item in env.get("DNS_RESOLVERS", "").split(",") if item.strip()
        )
        mode = env.get("DNS_VERIFICATION_MODE", "public").strip().lower() or "public"
        if mode not in ("public", "local"):
            raise ValueError("DNS_VERIFICATION_MODE must be 'public' or 'local'")
        return cls(
            verification_mode=mode,
            worker_enabled=enabled,
            worker_interval=max(1.0, float(env.get("DNS_WORKER_INTERVAL", "10"))),
            batch_size=max(1, int(env.get("DNS_WORKER_BATCH", "50"))),
            nameservers=nameservers,
            timeout=max(0.5, float(env.get("DNS_TIMEOUT", "5"))),
        )
