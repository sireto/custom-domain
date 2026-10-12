"""Domain Connect discovery: does the zone's DNS provider support our template?

Discovery follows the Domain Connect specification:

1. ``TXT _domainconnect.<zone>`` names the provider's API host (and
   sometimes a path), for example ``api.cloudflare.com/client/v4/dns/domainconnect``;
2. ``GET https://<that>/v2/<zone>/settings`` returns the provider's settings
   (``providerId``, ``providerName``, ``providerDisplayName``, ``urlSyncUX``,
   ``urlAPI``);
3. ``GET <urlAPI>/v2/domainTemplates/providers/<providerId>/services/<serviceId>``
   answers 2xx when the provider has the service's template.

The URLs come from DNS the customer controls, so every request goes through
the same guard as origin verification (``fetch_token``): https only, the
host resolved on every request and refused unless every address is public
(``ORIGIN_ALLOW_PRIVATE`` does not apply here), the connection pinned to the
checked address, TLS verified, redirects not followed, short timeouts with
an overall limit, and the body capped. Nothing is ever sent but a plain GET:
no credential, cookie or token. Values taken from the answers are length
limited and stripped of control characters before they are stored, and the
pages that show them escape them.

Discovery runs only in the DNS worker, never while serving a request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from app.dns.resolver import DnsNameNotFound, DnsUnavailable, Resolver
from app.services.origin_verification import OriginVerificationFailed, fetch_token

USER_AGENT = "custom-domain-discovery/1"
TIMEOUT = 5.0
TOTAL_TIMEOUT = 10.0
MAX_SETTINGS_BYTES = 16 * 1024
MAX_TEMPLATE_BYTES = 64 * 1024
MAX_TEXT = 128
MAX_URL = 512

# The TXT value is a host name optionally followed by a path; no scheme,
# port, credentials, query or fragment.
_TXT_VALUE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?:/[A-Za-z0-9._~/-]{0,200})?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Codes after which discovery is worth retrying sooner than the normal TTL.
TRANSIENT_CODES = frozenset(
    {
        "dns_timeout",
        "timeout",
        "connection_failed",
        "dns_resolution_failed",
        "tls_handshake_failed",
        "settings_unavailable",
        "template_check_failed",
    }
)


@dataclass(frozen=True)
class HttpAnswer:
    status: int
    body: str


Fetch = Callable[[str, int], HttpAnswer]
"""``fetch(url, max_bytes)``: one guarded GET; raises ``DiscoveryFailed``."""


class DiscoveryFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Discovery:
    """What discovery learned about one zone.

    ``settings`` is None when the zone has no Domain Connect record or its
    settings could not be read. ``template_supported`` is None when it was
    not asked (no template configured, or discovery stopped earlier).
    """

    settings: dict[str, str] | None = None
    template_supported: bool | None = None
    error_code: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def transient(self) -> bool:
        return self.error_code in TRANSIENT_CODES


def _clean(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL.sub("", value).strip()
    return cleaned[:limit] or None


def https_url(value: Any) -> str | None:
    """``value`` if it is an absolute https URL without credentials, else None."""
    if not isinstance(value, str) or len(value) > MAX_URL or _CONTROL.search(value):
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if port not in (None, 443) or parts.query or parts.fragment:
        return None
    return value.strip().rstrip("/")


def guarded_fetch(url: str, max_bytes: int) -> HttpAnswer:
    """GET ``url`` through the public-address guard; see the module docstring."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise DiscoveryFailed("url_refused", f"Not a usable URL: {exc}") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise DiscoveryFailed("url_refused", "Only https URLs without credentials are fetched")
    if port not in (None, 443):
        raise DiscoveryFailed("url_refused", "Only the default https port is used")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        probe = fetch_token(
            parts.hostname,
            443,
            "https",
            allow_private=False,
            path=path,
            timeout=TIMEOUT,
            max_bytes=max_bytes,
            accept="application/json",
            user_agent=USER_AGENT,
            total_timeout=TOTAL_TIMEOUT,
        )
    except OriginVerificationFailed as exc:
        if exc.code == "private_address_blocked":
            raise DiscoveryFailed(
                exc.code,
                f"{parts.hostname} resolves to a non-public address; discovery only "
                "contacts public addresses",
            ) from exc
        raise DiscoveryFailed(exc.code, exc.message) from exc
    return HttpAnswer(probe.status, probe.body)


def provider_api_host(values: list[str]) -> str | None:
    """The first usable host (and path) among the ``_domainconnect`` TXT values."""
    for raw in values:
        value = raw.strip().strip('"').strip().rstrip("/")
        if value.lower().startswith("https://"):
            value = value[len("https://") :]
        host, _, path = value.partition("/")
        candidate = host.lower() + (f"/{path}" if path else "")
        if _TXT_VALUE.match(candidate) and "." in host:
            return candidate
    return None


def parse_settings(body: str) -> dict[str, str]:
    """The settings fields discovery keeps; raises ``DiscoveryFailed`` when unusable."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DiscoveryFailed("settings_invalid", "The settings answer is not JSON") from exc
    if not isinstance(data, dict):
        raise DiscoveryFailed("settings_invalid", "The settings answer is not a JSON object")
    provider_id = _clean(data.get("providerId"))
    url_api = https_url(data.get("urlAPI"))
    url_sync_ux = https_url(data.get("urlSyncUX"))
    if provider_id is None or url_api is None:
        raise DiscoveryFailed(
            "settings_invalid", "The settings answer lacks providerId or an https urlAPI"
        )
    settings = {
        "provider_id": provider_id,
        "provider_name": _clean(data.get("providerName")),
        "provider_display_name": _clean(data.get("providerDisplayName")),
        "url_api": url_api,
        "url_sync_ux": url_sync_ux,
    }
    return {key: value for key, value in settings.items() if value is not None}


def discover(
    resolver: Resolver,
    zone: str,
    *,
    provider_id: str | None,
    service_id: str,
    fetch: Fetch = guarded_fetch,
) -> Discovery:
    """Run discovery for ``zone``. Never raises: failures are reported in the result.

    ``provider_id`` and ``service_id`` name the template to check; with no
    ``provider_id`` the template check is skipped.
    """
    record = f"_domainconnect.{zone}"
    try:
        values = resolver.txt(record)
    except DnsNameNotFound:
        values = []
    except DnsUnavailable as exc:
        return Discovery(error_code="dns_timeout", message=f"TXT lookup for {record} failed: {exc}")
    if not values:
        return Discovery(error_code="no_record", message=f"No TXT record at {record}")
    api_host = provider_api_host(values)
    if api_host is None:
        return Discovery(
            error_code="txt_invalid",
            message=f"The TXT record at {record} does not name a usable host",
        )

    try:
        answer = fetch(f"https://{api_host}/v2/{zone}/settings", MAX_SETTINGS_BYTES)
    except DiscoveryFailed as exc:
        return Discovery(error_code=exc.code, message=f"Settings request failed: {exc.message}")
    if answer.status == 404:
        return Discovery(
            error_code="settings_not_found",
            message=f"The provider at {api_host} has no Domain Connect settings for {zone}",
        )
    if 300 <= answer.status < 400:
        return Discovery(
            error_code="redirect_refused",
            message=f"The settings request was redirected (HTTP {answer.status}); "
            "redirects are not followed",
        )
    if answer.status != 200:
        return Discovery(
            error_code="settings_unavailable",
            message=f"The settings request returned HTTP {answer.status}",
        )
    try:
        settings = parse_settings(answer.body)
    except DiscoveryFailed as exc:
        return Discovery(error_code=exc.code, message=exc.message)

    if not provider_id:
        return Discovery(settings=settings)
    if not settings.get("url_sync_ux"):
        return Discovery(
            settings=settings,
            template_supported=False,
            error_code="settings_invalid",
            message="The settings answer lacks an https urlSyncUX, so changes cannot be applied",
        )
    template_url = (
        f"{settings['url_api']}/v2/domainTemplates/providers/{quote(provider_id, safe='')}"
        f"/services/{quote(service_id, safe='')}"
    )
    try:
        answer = fetch(template_url, MAX_TEMPLATE_BYTES)
    except DiscoveryFailed as exc:
        return Discovery(
            settings=settings,
            error_code=exc.code,
            message=f"Template check failed: {exc.message}",
        )
    if 200 <= answer.status < 300:
        return Discovery(settings=settings, template_supported=True)
    if answer.status == 404:
        return Discovery(
            settings=settings,
            template_supported=False,
            error_code="template_not_supported",
            message="The provider supports Domain Connect but not this service's template",
        )
    if 300 <= answer.status < 400:
        return Discovery(
            settings=settings,
            error_code="redirect_refused",
            message=f"The template check was redirected (HTTP {answer.status}); "
            "redirects are not followed",
        )
    return Discovery(
        settings=settings,
        error_code="template_check_failed",
        message=f"The template check returned HTTP {answer.status}",
    )
