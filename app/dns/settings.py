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


DEFAULT_GUIDE_URL = (
    "https://github.com/sireto/custom-domain/blob/main/docs/dns-guides/{provider}.md"
)


@dataclass(frozen=True)
class SetupSettings:
    """How the service detects and reports each domain's DNS setup.

    ``template_provider_id`` and ``template_service_id`` name the Domain
    Connect template whose support discovery checks. There is no default
    provider id: a deployment that has published a template sets
    ``DOMAIN_CONNECT_PROVIDER_ID``; without it discovery still identifies the
    provider but never reports the template as supported.

    ``signing_enabled`` is whether the service can sign Domain Connect apply
    requests; ``setup.method`` is ``domain_connect`` only when it can. No
    signer is configurable yet, so it stays off.

    ``guide_url`` is the address of a provider's setup guide, with
    ``{provider}`` replaced by the provider id (or ``generic``).
    """

    template_provider_id: str = ""
    template_service_id: str = "hostname"
    signing_enabled: bool = False
    guide_url: str = DEFAULT_GUIDE_URL

    @property
    def template_key(self) -> str | None:
        if not self.template_provider_id:
            return None
        return f"{self.template_provider_id}/{self.template_service_id}"

    def guide_for(self, slug: str) -> str:
        return self.guide_url.replace("{provider}", slug)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SetupSettings:
        env = os.environ if environ is None else environ
        provider_id = env.get("DOMAIN_CONNECT_PROVIDER_ID", "").strip()
        service_id = env.get("DOMAIN_CONNECT_SERVICE_ID", "").strip() or "hostname"
        for name, value in (("PROVIDER_ID", provider_id), ("SERVICE_ID", service_id)):
            if len(value) > 64 or any(c.isspace() or c == "/" for c in value):
                raise ValueError(
                    f"DOMAIN_CONNECT_{name} must be at most 64 characters, "
                    "without spaces or slashes"
                )
        guide_url = env.get("DNS_GUIDE_URL", "").strip() or DEFAULT_GUIDE_URL
        if not guide_url.startswith(("https://", "http://")) or "{provider}" not in guide_url:
            raise ValueError("DNS_GUIDE_URL must be an http(s) URL containing {provider}")
        return cls(
            template_provider_id=provider_id,
            template_service_id=service_id,
            guide_url=guide_url,
        )
