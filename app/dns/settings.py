from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


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
    requests (``SignerSettings.enabled``); ``setup.method`` is
    ``domain_connect`` only when it can. A signer set through the operator API
    overrides both the template and this flag
    (app/services/domain_connect.py, ``effective_setup_settings``).

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
            signing_enabled=bool(provider_id) and SignerSettings.from_env(env).enabled,
            guide_url=guide_url,
        )


SIGNERS = ("local", "remote")


@dataclass(frozen=True)
class SignerSettings:
    """``DOMAIN_CONNECT_SIGNER`` and its companions (docs/domain-connect.md).

    * unset or ``none``: Domain Connect is off;
    * ``local``: sign with ``DOMAIN_CONNECT_PRIVATE_KEY`` (or the file named by
      ``DOMAIN_CONNECT_PRIVATE_KEY_FILE``) and ``DOMAIN_CONNECT_KEY_HOST``;
    * ``remote``: ask the signing service at ``DOMAIN_CONNECT_SIGNER_URL``
      with ``DOMAIN_CONNECT_SIGNER_CREDENTIAL``.

    ``DOMAIN_CONNECT_RETURN_URL`` is where providers send the customer back
    (``/v1/domain-connect/return`` on a public address of this service).

    The key and the credential are kept out of ``repr`` and never logged.
    The key is parsed here so a bad value stops the service at start.
    """

    signer: str | None = None
    private_key: str | None = field(default=None, repr=False)
    key_host: str | None = None
    url: str | None = None
    credential: str | None = field(default=None, repr=False)
    return_url: str | None = None

    @property
    def enabled(self) -> bool:
        if self.signer == "local":
            return bool(self.private_key)
        if self.signer == "remote":
            return bool(self.url and self.credential)
        return False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SignerSettings:
        from app.dns.signing import CREDENTIAL, https_url_problem, parse_private_keys

        env = os.environ if environ is None else environ
        signer = env.get("DOMAIN_CONNECT_SIGNER", "").strip().lower() or None
        if signer == "none":
            signer = None
        if signer is not None and signer not in SIGNERS:
            raise ValueError("DOMAIN_CONNECT_SIGNER must be local, remote or none")
        return_url = env.get("DOMAIN_CONNECT_RETURN_URL", "").strip() or None
        if return_url is not None and https_url_problem(return_url):
            raise ValueError("DOMAIN_CONNECT_RETURN_URL must be an https URL")
        if signer == "local":
            key = env.get("DOMAIN_CONNECT_PRIVATE_KEY", "").strip()
            key_file = env.get("DOMAIN_CONNECT_PRIVATE_KEY_FILE", "").strip()
            if key and key_file:
                raise ValueError(
                    "Set DOMAIN_CONNECT_PRIVATE_KEY or DOMAIN_CONNECT_PRIVATE_KEY_FILE, not both"
                )
            if key_file:
                try:
                    key = Path(key_file).read_text().strip()
                except OSError as exc:
                    raise ValueError(
                        f"DOMAIN_CONNECT_PRIVATE_KEY_FILE cannot be read ({exc.strerror})"
                    ) from None
            if not key:
                raise ValueError(
                    "DOMAIN_CONNECT_SIGNER=local needs DOMAIN_CONNECT_PRIVATE_KEY "
                    "or DOMAIN_CONNECT_PRIVATE_KEY_FILE"
                )
            key_host = env.get("DOMAIN_CONNECT_KEY_HOST", "").strip() or None
            parse_private_keys(key, key_host)  # InvalidSigningKey is a ValueError
            return cls(signer, private_key=key, key_host=key_host, return_url=return_url)
        if signer == "remote":
            url = env.get("DOMAIN_CONNECT_SIGNER_URL", "").strip()
            credential = env.get("DOMAIN_CONNECT_SIGNER_CREDENTIAL", "").strip()
            if not url or https_url_problem(url):
                raise ValueError(
                    "DOMAIN_CONNECT_SIGNER=remote needs an https DOMAIN_CONNECT_SIGNER_URL"
                )
            if not CREDENTIAL.match(credential):
                raise ValueError(
                    "DOMAIN_CONNECT_SIGNER=remote needs DOMAIN_CONNECT_SIGNER_CREDENTIAL "
                    "(dcs_ followed by 43 characters)"
                )
            return cls(signer, url=url, credential=credential, return_url=return_url)
        return cls(None, return_url=return_url)
