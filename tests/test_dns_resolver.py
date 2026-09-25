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


# --- local mode: answers from the records the service issued -----------------


def test_issued_records_resolver_answers_the_issued_records(
    session, session_factory, make_application
):
    from app.dns.resolver import DnsNameNotFound, IssuedRecordsResolver, cname_chain
    from app.services.domains import claim_domain, delete_domain, reissue_claim

    acme = make_application("acme", cname_target="acme.edge.example.net")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    claim = domain.active_claim
    resolver = IssuedRecordsResolver(session_factory)

    assert resolver.txt(claim.txt_record_name) == [claim.txt_record_value]
    assert resolver.txt(claim.txt_record_name.upper() + ".") == [claim.txt_record_value]
    assert resolver.cname("forms.customer.example") == "acme.edge.example.net"
    assert cname_chain(resolver, "forms.customer.example", stop_at="acme.edge.example.net") == [
        "acme.edge.example.net"
    ]
    assert resolver.has_address("forms.customer.example") is False
    with pytest.raises(DnsNameNotFound):
        resolver.txt("_custom-domain-challenge.nobody.example")
    with pytest.raises(DnsNameNotFound):
        resolver.cname("nobody.example")

    # A re-issued claim: the old value is no longer answered, the new one is.
    old_value = claim.txt_record_value
    reissue_claim(session, acme, domain.id)
    session.commit()
    session.expire_all()
    fresh = domain.active_claim
    assert resolver.txt(fresh.txt_record_name) == [fresh.txt_record_value]
    assert old_value not in resolver.txt(fresh.txt_record_name)

    # A deleted domain has no records.
    delete_domain(session, acme, domain.id)
    session.commit()
    with pytest.raises(DnsNameNotFound):
        resolver.txt(fresh.txt_record_name)
    with pytest.raises(DnsNameNotFound):
        resolver.cname("forms.customer.example")


def test_issued_records_resolver_drives_the_dns_checks(session, session_factory, make_application):
    from app.dns.resolver import IssuedRecordsResolver
    from app.models import ClaimStatus, DomainStatus
    from app.services.dns_checks import run_dns_checks
    from app.services.domains import claim_domain

    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    run_dns_checks(session, domain, IssuedRecordsResolver(session_factory))
    session.commit()
    assert domain.status == DomainStatus.PROVISIONING
    assert domain.active_claim.status == ClaimStatus.VERIFIED


def test_local_mode_requires_a_local_edge(session_factory):
    from app.dns.resolver import IssuedRecordsResolver, SystemResolver, make_resolver
    from app.dns.settings import DnsSettings
    from app.edge.settings import EdgeConfigurationError, EdgeSettings

    local = DnsSettings.from_env({"DNS_VERIFICATION_MODE": "local"})
    assert local.verification_mode == "local"
    with pytest.raises(EdgeConfigurationError):
        make_resolver(local, EdgeSettings(), session_factory)
    assert isinstance(
        make_resolver(local, EdgeSettings(disable_https=True), session_factory),
        IssuedRecordsResolver,
    )
    assert isinstance(
        make_resolver(local, EdgeSettings(tls_issuer="internal"), session_factory),
        IssuedRecordsResolver,
    )
    public = DnsSettings.from_env({})
    assert isinstance(make_resolver(public, EdgeSettings(), session_factory), SystemResolver)
    with pytest.raises(ValueError):
        DnsSettings.from_env({"DNS_VERIFICATION_MODE": "trust-me"})
