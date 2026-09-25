import pytest

from app.dns.resolver import DnsNameNotFound, DnsUnavailable, SystemResolver, normalize_name
from app.dns.settings import DnsSettings


class _Answer:
    def __init__(self, rrset):
        self.rrset = rrset


def test_system_resolver_maps_dnspython_outcomes(monkeypatch):
    import dns.rdatatype
    import dns.resolver

    resolver = SystemResolver(["192.0.2.53"], timeout=1)
    assert resolver._resolver.nameservers == ["192.0.2.53"]

    class TxtRecord:
        strings = (b"custom-domain-", b"verify=abc")

    class CnameTarget:
        target = "Acme.Edge.Example.NET."

    class Rrset(list):
        rdtype = dns.rdatatype.CNAME

    def fake_resolve(name, rdtype, raise_on_no_answer=False, search=False):
        if name == "nx.example":
            raise dns.resolver.NXDOMAIN
        if name == "slow.example":
            raise dns.resolver.LifetimeTimeout
        if name == "dead.example":
            raise dns.resolver.NoNameservers
        if rdtype == "TXT":
            return _Answer([TxtRecord()] if name == "txt.example" else None)
        if rdtype == "CNAME":
            rrset = Rrset([CnameTarget()]) if name == "alias.example" else None
            return _Answer(rrset)
        return _Answer([object()] if name == "addr.example" and rdtype == "A" else None)

    monkeypatch.setattr(resolver._resolver, "resolve", fake_resolve)
    assert resolver.txt("txt.example") == ["custom-domain-verify=abc"]
    assert resolver.txt("alias.example") == []
    assert resolver.cname("alias.example") == "acme.edge.example.net"
    assert resolver.cname("txt.example") is None
    assert resolver.has_address("addr.example") is True
    assert resolver.has_address("txt.example") is False
    with pytest.raises(DnsNameNotFound):
        resolver.txt("nx.example")
    with pytest.raises(DnsUnavailable):
        resolver.cname("slow.example")
    with pytest.raises(DnsUnavailable):
        resolver.txt("dead.example")
    assert normalize_name("Foo.Example.") == "foo.example"


def test_dns_settings_from_env():
    settings = DnsSettings.from_env({})
    assert settings.worker_enabled and settings.nameservers == () and settings.timeout == 5.0
    settings = DnsSettings.from_env(
        {
            "DNS_WORKER_ENABLED": "no",
            "DNS_RESOLVERS": "1.1.1.1, 9.9.9.9",
            "DNS_TIMEOUT": "0.1",
            "DNS_WORKER_INTERVAL": "0",
        }
    )
    assert not settings.worker_enabled
    assert settings.nameservers == ("1.1.1.1", "9.9.9.9")
    assert settings.timeout == 0.5 and settings.worker_interval == 1.0
