"""Edge settings read from the environment.

The database is the source of truth for what the edge serves; these settings
only say where Caddy is, how it stores certificates, and how often the
reconciler runs. See docs/decisions/0001-certificate-storage.md.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

DEFAULT_ADMIN_URL = "http://localhost:2019"
DEFAULT_HTTPS_PORT = 443
DEFAULT_RECONCILE_INTERVAL = 30.0
DEFAULT_ASK_URL = "http://localhost:9000/internal/tls/ask"
DEFAULT_PROBE_TIMEOUT = 15.0
HEALTH_PATH = "/.well-known/custom-domain-edge-health"
WORKSPACE_PATH = "/.well-known/custom-domain-workspace"
DEFAULT_ASSERT_UPSTREAM = "localhost:9000"
ASSERT_PATH = "/internal/edge/assert"
REDIS_ENCRYPTION_KEY_LENGTH = 32

StorageKind = Literal["file", "redis"]


class EdgeConfigurationError(ValueError):
    pass


def _flag(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _key_pairs(spec: str) -> tuple[tuple[str, str], ...]:
    from app.edge.assertion import parse_keys

    try:
        parsed = parse_keys(spec)
    except ValueError as exc:
        raise EdgeConfigurationError(str(exc)) from exc
    return tuple((key_id, secret.decode("utf-8")) for key_id, secret in parsed.items())


def _csv(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in environ.get(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class EdgeSettings:
    admin_url: str = DEFAULT_ADMIN_URL
    https_port: int = DEFAULT_HTTPS_PORT
    # Port for the HTTP-to-HTTPS redirect listener Caddy adds automatically.
    http_port: int = 80
    disable_https: bool = False
    acme_email: str | None = None
    # "acme" (public CA) or "internal" (Caddy's local CA; development and
    # staging only, the probe must trust its root via EDGE_PROBE_CA_FILE).
    tls_issuer: str = "acme"
    storage: StorageKind = "file"
    redis_address: tuple[str, ...] = ()
    redis_username: str | None = None
    redis_password: str | None = None
    redis_db: int = 0
    redis_key_prefix: str = "caddy"
    redis_encryption_key: str | None = None
    redis_tls: bool = False
    redis_tls_server_certs_pem: str | None = None
    reconcile_enabled: bool = False
    reconcile_interval: float = DEFAULT_RECONCILE_INTERVAL
    legacy_api_enabled: bool = True
    # On-demand TLS: Caddy asks this URL before issuing a certificate.
    ask_url: str = DEFAULT_ASK_URL
    ask_trusted_hosts: tuple[str, ...] = ("127.0.0.1", "::1")
    # Readiness probe: where to connect (default: the hostname itself) and
    # which CA file to trust (default: system store).
    probe_address: str | None = None
    probe_ca_file: str | None = None
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT
    # Routing: Caddy asks this upstream for the signed assertion on every
    # request; the keys sign it (first entry is active, others verify only).
    assert_upstream: str = DEFAULT_ASSERT_UPSTREAM
    assertion_keys: tuple[tuple[str, str], ...] = ()
    assertion_ttl: int = 60
    _validated: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EdgeSettings:
        env = os.environ if environ is None else environ
        legacy = _flag(env, "ENABLE_LEGACY_API", True)
        storage = env.get("CADDY_STORAGE", "file").strip().lower() or "file"
        if storage not in ("file", "redis"):
            raise EdgeConfigurationError("CADDY_STORAGE must be 'file' or 'redis'")
        settings = cls(
            admin_url=env.get("CADDY_ADMIN_URL", DEFAULT_ADMIN_URL).rstrip("/"),
            https_port=int(env.get("EDGE_HTTPS_PORT", DEFAULT_HTTPS_PORT)),
            http_port=int(env.get("EDGE_HTTP_PORT", "80")),
            disable_https=_flag(env, "DISABLE_HTTPS", False),
            acme_email=env.get("ACME_EMAIL", "").strip() or None,
            tls_issuer=env.get("EDGE_TLS_ISSUER", "acme").strip().lower() or "acme",
            storage=storage,  # type: ignore[arg-type]
            redis_address=_csv(env, "CADDY_REDIS_ADDRESS"),
            redis_username=env.get("CADDY_REDIS_USERNAME") or None,
            redis_password=env.get("CADDY_REDIS_PASSWORD") or None,
            redis_db=int(env.get("CADDY_REDIS_DB", "0")),
            redis_key_prefix=env.get("CADDY_REDIS_KEY_PREFIX", "caddy").strip() or "caddy",
            redis_encryption_key=env.get("CADDY_REDIS_ENCRYPTION_KEY") or None,
            redis_tls=_flag(env, "CADDY_REDIS_TLS", False),
            redis_tls_server_certs_pem=env.get("CADDY_REDIS_TLS_SERVER_CERTS_PEM") or None,
            # The reconciler owns the Caddy configuration. It must not run while
            # the legacy API still writes its own config, so it defaults to the
            # opposite of ENABLE_LEGACY_API.
            reconcile_enabled=_flag(env, "EDGE_RECONCILE_ENABLED", not legacy),
            reconcile_interval=float(
                env.get("EDGE_RECONCILE_INTERVAL", DEFAULT_RECONCILE_INTERVAL)
            ),
            legacy_api_enabled=legacy,
            ask_url=env.get("EDGE_ASK_URL", DEFAULT_ASK_URL).strip() or DEFAULT_ASK_URL,
            ask_trusted_hosts=_csv(env, "EDGE_ASK_TRUSTED_HOSTS") or ("127.0.0.1", "::1"),
            probe_address=env.get("EDGE_PROBE_ADDRESS", "").strip() or None,
            probe_ca_file=env.get("EDGE_PROBE_CA_FILE", "").strip() or None,
            probe_timeout=float(env.get("EDGE_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT)),
            assert_upstream=env.get("EDGE_ASSERT_UPSTREAM", DEFAULT_ASSERT_UPSTREAM).strip()
            or DEFAULT_ASSERT_UPSTREAM,
            assertion_keys=_key_pairs(env.get("EDGE_ASSERTION_KEYS", "")),
            assertion_ttl=int(env.get("EDGE_ASSERTION_TTL", "60")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.reconcile_enabled and self.legacy_api_enabled:
            raise EdgeConfigurationError(
                "EDGE_RECONCILE_ENABLED and ENABLE_LEGACY_API cannot both be true: the legacy "
                "/domains API and the reconciler would overwrite each other's Caddy config. "
                "Set ENABLE_LEGACY_API=false once the legacy hostnames are imported."
            )
        if self.storage == "redis":
            if not self.redis_address:
                raise EdgeConfigurationError("CADDY_STORAGE=redis requires CADDY_REDIS_ADDRESS")
            key = self.redis_encryption_key
            if key is not None and len(key) < REDIS_ENCRYPTION_KEY_LENGTH:
                raise EdgeConfigurationError(
                    f"CADDY_REDIS_ENCRYPTION_KEY must be at least {REDIS_ENCRYPTION_KEY_LENGTH} "
                    "characters"
                )
        if self.reconcile_interval < 1:
            raise EdgeConfigurationError("EDGE_RECONCILE_INTERVAL must be at least 1 second")
        if not 0 < self.https_port < 65536:
            raise EdgeConfigurationError("EDGE_HTTPS_PORT must be between 1 and 65535")
        if not 0 < self.http_port < 65536 or self.http_port == self.https_port:
            raise EdgeConfigurationError("EDGE_HTTP_PORT must be a valid port different from HTTPS")
        if self.tls_issuer not in ("acme", "internal"):
            raise EdgeConfigurationError("EDGE_TLS_ISSUER must be 'acme' or 'internal'")
        if self.probe_timeout < 1:
            raise EdgeConfigurationError("EDGE_PROBE_TIMEOUT must be at least 1 second")
        if self.probe_address and ":" not in self.probe_address:
            raise EdgeConfigurationError("EDGE_PROBE_ADDRESS must be host:port")
        if self.reconcile_enabled and not self.assertion_keys:
            raise EdgeConfigurationError(
                "EDGE_ASSERTION_KEYS is required when the edge is enabled: routing signs every "
                "proxied request (docs/edge-routing.md)"
            )
        if not 5 <= self.assertion_ttl <= 600:
            raise EdgeConfigurationError("EDGE_ASSERTION_TTL must be between 5 and 600 seconds")

    def signing_keys(self) -> dict[str, bytes]:
        return {key_id: secret.encode("utf-8") for key_id, secret in self.assertion_keys}

    def active_key(self) -> tuple[str, bytes]:
        key_id, secret = self.assertion_keys[0]
        return key_id, secret.encode("utf-8")

    def tls_issuer_config(self) -> dict[str, Any]:
        if self.tls_issuer == "internal":
            return {"module": "internal"}
        issuer: dict[str, Any] = {"module": "acme"}
        if self.acme_email:
            issuer["email"] = self.acme_email
        return issuer

    def storage_config(self) -> dict[str, Any] | None:
        """The Caddy ``storage`` block, or None for Caddy's default file storage."""
        if self.storage != "redis":
            return None
        block: dict[str, Any] = {
            "module": "redis",
            "client_type": "cluster" if len(self.redis_address) > 1 else "simple",
            "address": list(self.redis_address),
            "db": self.redis_db,
            "key_prefix": self.redis_key_prefix,
            "tls_enabled": self.redis_tls,
        }
        if self.redis_username:
            block["username"] = self.redis_username
        if self.redis_password:
            block["password"] = self.redis_password
        if self.redis_encryption_key:
            # The module uses the first 32 characters as the AES key.
            block["encryption_key"] = self.redis_encryption_key[:REDIS_ENCRYPTION_KEY_LENGTH]
        if self.redis_tls_server_certs_pem:
            block["tls_server_certs_pem"] = self.redis_tls_server_certs_pem
        return block


SECRET_STORAGE_KEYS = ("password", "encryption_key", "tls_server_certs_pem")


def redact(config: dict[str, Any]) -> dict[str, Any]:
    """Copy of a Caddy config with storage secrets masked, for logs and CLI output."""
    import copy

    masked = copy.deepcopy(config)
    storage = masked.get("storage")
    if isinstance(storage, dict):
        for key in SECRET_STORAGE_KEYS:
            if storage.get(key):
                storage[key] = "***"
    return masked
