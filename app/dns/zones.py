"""Where a customer hostname's records live: the zone root and the host part.

DNS providers and Domain Connect both address a record as a *host* inside a
*zone*: ``forms.eu`` in ``acme.co.uk`` for ``forms.eu.acme.co.uk``. The
registrable domain from the Public Suffix List is the starting point (the
list ships with the pinned ``publicsuffixlist`` dependency and is updated
with it). The DNS worker refines it by walking up from the hostname's parent
to the registrable domain and taking the first name that has its own NS
records, so a customer who delegated ``eu.acme.co.uk`` to another provider
gets that zone. The walk never goes above the registrable domain: public
suffixes are not customer zones.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.dns.resolver import DnsNameNotFound, DnsUnavailable, Resolver


def registrable_domain(hostname: str) -> str | None:
    """The registrable domain of ``hostname`` per the Public Suffix List, if any."""
    from app.hostname import _PSL

    return _PSL.privatesuffix(hostname.rstrip(".").lower())


def default_zone(hostname: str) -> str:
    """The zone assumed before DNS says otherwise: the registrable domain.

    Falls back to the hostname's parent for names the list does not cover
    (which ``canonicalize`` does not accept for new domains).
    """
    hostname = hostname.rstrip(".").lower()
    zone = registrable_domain(hostname)
    if zone is not None and zone != hostname:
        return zone
    return hostname.split(".", 1)[1] if "." in hostname else hostname


def host_in_zone(hostname: str, zone: str) -> str:
    """``hostname`` relative to ``zone``; ``@`` for the zone root itself."""
    hostname = hostname.rstrip(".").lower()
    zone = zone.rstrip(".").lower()
    if hostname == zone:
        return "@"
    if hostname.endswith("." + zone):
        return hostname[: -(len(zone) + 1)]
    raise ValueError(f"{hostname} is not inside {zone}")


def candidate_zones(hostname: str) -> list[str]:
    """Names that may be the zone root, closest first.

    From the hostname's parent up to the registrable domain. The hostname
    itself is excluded: it carries the routing CNAME, which a zone root
    cannot have.
    """
    hostname = hostname.rstrip(".").lower()
    top = default_zone(hostname)
    candidates: list[str] = []
    name = hostname
    while "." in name:
        name = name.split(".", 1)[1]
        if name != top and not name.endswith("." + top):
            break
        candidates.append(name)
        if name == top:
            break
    return candidates or [top]


@dataclass(frozen=True)
class ZoneLookup:
    zone: str
    nameservers: tuple[str, ...]
    error_code: str | None = None
    message: str | None = None

    @property
    def transient(self) -> bool:
        return self.error_code == "dns_timeout"


def find_zone(resolver: Resolver, hostname: str) -> ZoneLookup:
    """The closest enclosing zone of ``hostname`` and its nameservers.

    Falls back to the registrable domain without nameservers when none of
    the candidates has NS records or DNS is unavailable; the error says why.
    """
    candidates = candidate_zones(hostname)
    for name in candidates:
        try:
            nameservers = resolver.ns(name)
        except DnsNameNotFound:
            continue
        except DnsUnavailable as exc:
            return ZoneLookup(
                candidates[-1], (), "dns_timeout", f"NS lookup for {name} failed: {exc}"
            )
        if nameservers:
            cleaned = sorted({ns.rstrip(".").lower() for ns in nameservers if ns})
            return ZoneLookup(name, tuple(cleaned))
    return ZoneLookup(
        candidates[-1],
        (),
        "nameservers_not_found",
        f"No NS records were found for {candidates[-1]}; is the domain registered?",
    )
