"""The DNS providers the service recognizes, keyed by stable ids.

The DNS worker identifies the provider hosting a customer's zone from the
zone's nameservers (``ns_patterns``), or, when the nameservers say nothing,
from the ``providerId`` a Domain Connect settings document reports
(``domain_connect_ids``). The ids are part of the v1 API (``setup.provider.id``),
the operator usage counts and the guide file names under ``docs/dns-guides/``,
so they never change once published; add new providers instead.

``guide`` marks providers with a provider-specific setup guide; customers of
the others are pointed at the generic guide (``GENERIC_GUIDE``).

Patterns are shell-style globs (``fnmatch``) matched against lowercase
nameserver names without the trailing dot.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase

UNKNOWN = "unknown"
GENERIC_GUIDE = "generic"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    ns_patterns: tuple[str, ...] = ()
    domain_connect_ids: tuple[str, ...] = ()
    guide: bool = False

    @property
    def guide_slug(self) -> str:
        return self.id if self.guide else GENERIC_GUIDE


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        "cloudflare",
        "Cloudflare",
        ("*.ns.cloudflare.com",),
        ("cloudflare.com", "cloudflare"),
        guide=True,
    ),
    Provider(
        "godaddy",
        "GoDaddy",
        ("*.domaincontrol.com",),
        ("godaddy.com", "godaddy"),
        guide=True,
    ),
    Provider(
        "namecheap",
        "Namecheap",
        ("dns*.registrar-servers.com",),
        guide=True,
    ),
    Provider(
        "route53",
        "Amazon Route 53",
        ("ns-*.awsdns-*",),
        guide=True,
    ),
    Provider(
        "google-cloud-dns",
        "Google Cloud DNS",
        ("ns-cloud-*.googledomains.com",),
        guide=True,
    ),
    Provider(
        "squarespace",
        "Squarespace",
        ("*.squarespacedns.com",),
        guide=True,
    ),
    Provider(
        "porkbun",
        "Porkbun",
        ("*.porkbun.com",),
        guide=True,
    ),
    Provider(
        "hostinger",
        "Hostinger",
        ("*.dns-parking.com",),
        guide=True,
    ),
    Provider(
        "ionos",
        "IONOS",
        ("*.ui-dns.com", "*.ui-dns.de", "*.ui-dns.org", "*.ui-dns.biz"),
        ("ionos.com", "ionos", "1and1.com"),
        guide=True,
    ),
    Provider(
        "digitalocean",
        "DigitalOcean",
        ("ns*.digitalocean.com",),
        guide=True,
    ),
    Provider(
        "vercel",
        "Vercel",
        ("*.vercel-dns.com",),
        ("vercel.com", "vercel"),
        guide=True,
    ),
    Provider(
        "netlify",
        "Netlify",
        # Netlify DNS runs on NS1's pXX pools; NS1's own customers use the
        # same names, so a zone hosted directly at NS1 is reported as Netlify.
        ("dns*.p*.nsone.net",),
        guide=True,
    ),
    Provider("namesilo", "NameSilo", ("*.dnsowl.com",), ("namesilo.com", "namesilo")),
    Provider("wordpress-com", "WordPress.com", ("ns*.wordpress.com",), ("wordpress.com",)),
    Provider("azure-dns", "Azure DNS", ("ns*-*.azure-dns.*",)),
    Provider("gandi", "Gandi", ("*.gandi.net",)),
    Provider("ovh", "OVHcloud", ("*.ovh.net", "*.ovh.ca")),
    Provider(
        "hetzner",
        "Hetzner",
        ("*.ns.hetzner.com", "*.ns.hetzner.de", "*.your-server.de", "*.second-ns.de"),
    ),
    Provider(
        "dnsimple", "DNSimple", ("*.dnsimple.com", "*.dnsimple-edge.net", "*.dnsimple-edge.org")
    ),
    Provider("name-com", "Name.com", ("ns*.name.com",)),
    Provider("linode", "Akamai Cloud (Linode)", ("ns*.linode.com",)),
    Provider("wix", "Wix", ("*.wixdns.net",)),
    Provider(UNKNOWN, "Unknown provider"),
)

_BY_ID = {provider.id: provider for provider in PROVIDERS}


def get_provider(provider_id: str | None) -> Provider:
    """The provider with this id; the unknown provider for anything else."""
    return _BY_ID.get(provider_id or "", _BY_ID[UNKNOWN])


def _normalize(name: str) -> str:
    return name.strip().rstrip(".").lower()


def provider_for_nameserver(nameserver: str) -> Provider | None:
    name = _normalize(nameserver)
    for provider in PROVIDERS:
        if any(fnmatchcase(name, pattern) for pattern in provider.ns_patterns):
            return provider
    return None


def provider_for_nameservers(nameservers: Iterable[str]) -> Provider:
    """The provider most of the zone's nameservers belong to, or the unknown provider.

    Zones are sometimes served by two providers at once (a secondary DNS
    service, or the middle of a migration); the one with more nameservers
    wins, and a tie goes to the first listed in ``PROVIDERS``.
    """
    votes: Counter[str] = Counter()
    for nameserver in nameservers:
        provider = provider_for_nameserver(nameserver)
        if provider is not None:
            votes[provider.id] += 1
    if not votes:
        return _BY_ID[UNKNOWN]
    order = {provider.id: index for index, provider in enumerate(PROVIDERS)}
    best = max(votes, key=lambda pid: (votes[pid], -order[pid]))
    return _BY_ID[best]


def provider_for_domain_connect(provider_id: str | None) -> Provider | None:
    """The provider a Domain Connect settings document's ``providerId`` names, if known."""
    if not provider_id:
        return None
    wanted = provider_id.strip().lower()
    for provider in PROVIDERS:
        if wanted in provider.domain_connect_ids:
            return provider
    return None
