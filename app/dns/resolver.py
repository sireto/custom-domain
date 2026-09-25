"""Resolver abstraction used by the DNS checks.

``Resolver`` is the small interface the checks need; ``SystemResolver`` is
the production implementation on dnspython, querying the configured
nameservers (the system's by default). Tests supply an in-memory resolver.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class DnsError(Exception):
    pass


class DnsNameNotFound(DnsError):
    """NXDOMAIN: the name does not exist at all."""


class DnsUnavailable(DnsError):
    """Timeout, SERVFAIL or no usable nameserver; retry later."""


class Resolver(Protocol):
    def txt(self, name: str) -> list[str]:
        """TXT strings at ``name`` (empty when the name has none)."""

    def cname(self, name: str) -> str | None:
        """Canonical CNAME target of ``name`` (lowercase, no trailing dot) or None."""

    def has_address(self, name: str) -> bool:
        """Whether ``name`` has A or AAAA records."""


MAX_CNAME_HOPS = 8


def cname_chain(
    resolver: Resolver,
    name: str,
    *,
    stop_at: str | None = None,
    max_hops: int = MAX_CNAME_HOPS,
) -> list[str]:
    """Follow CNAMEs from ``name``; returns the targets in order (possibly empty).

    Stops when ``stop_at`` is reached, at ``max_hops``, on loops, and when a
    later hop does not resolve (the chain is then reported as far as it went).
    Errors on the first hop propagate so the caller can distinguish a missing
    name from a broken chain.
    """
    chain: list[str] = []
    current = name
    for hop in range(max_hops):
        try:
            target = resolver.cname(current)
        except DnsError:
            if hop == 0:
                raise
            break
        if target is None or target in chain or target == current:
            break
        chain.append(target)
        if stop_at is not None and target == stop_at:
            break
        current = target
    return chain


def normalize_name(name: str) -> str:
    return name.rstrip(".").lower()


class SystemResolver:
    def __init__(self, nameservers: Sequence[str] | None = None, *, timeout: float = 5.0) -> None:
        import dns.resolver

        self._resolver = dns.resolver.Resolver(configure=not nameservers)
        if nameservers:
            self._resolver.nameservers = list(nameservers)
        self._resolver.timeout = timeout
        self._resolver.lifetime = timeout

    def _query(self, name: str, rdtype: str):
        import dns.exception
        import dns.resolver

        try:
            return self._resolver.resolve(name, rdtype, raise_on_no_answer=False, search=False)
        except dns.resolver.NXDOMAIN as exc:
            raise DnsNameNotFound(name) from exc
        except (dns.resolver.LifetimeTimeout, dns.exception.Timeout) as exc:
            raise DnsUnavailable(f"DNS query for {name} timed out") from exc
        except (dns.resolver.NoNameservers, dns.resolver.NoResolverConfiguration) as exc:
            raise DnsUnavailable(f"No usable nameserver for {name}: {exc}") from exc
        except dns.exception.DNSException as exc:
            raise DnsUnavailable(f"DNS query for {name} failed: {exc}") from exc

    def txt(self, name: str) -> list[str]:
        answer = self._query(name, "TXT")
        if answer.rrset is None:
            return []
        return [b"".join(record.strings).decode("utf-8", "replace") for record in answer.rrset]

    def cname(self, name: str) -> str | None:
        import dns.rdatatype

        answer = self._query(name, "CNAME")
        rrset = answer.rrset
        if rrset is None or rrset.rdtype != dns.rdatatype.CNAME:
            return None
        return normalize_name(str(rrset[0].target))

    def has_address(self, name: str) -> bool:
        return any(self._query(name, rdtype).rrset is not None for rdtype in ("A", "AAAA"))
