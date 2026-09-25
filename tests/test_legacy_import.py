from app.caddy import saas_template
from app.legacy import (
    LEGACY_IMPORT_METHOD,
    LegacyDomain,
    import_legacy_domains,
    parse_legacy_config,
)
from app.models import CheckStatus, CheckType, ClaimStatus, DomainStatus, EventType


def _legacy_config():
    config = saas_template.https_template()
    config = saas_template.add_https_domain(
        "forms.customer-one.example", "app.acme.example:443", template=config
    )
    config = saas_template.add_https_domain(
        "Forms.Customer-Two.Example", "app.acme.example", template=config
    )
    config = saas_template.add_https_domain("apex.example", "app.acme.example:443", template=config)
    return config


def test_parse_legacy_config_extracts_hosts_and_upstreams():
    entries = parse_legacy_config(_legacy_config())
    assert entries == [
        LegacyDomain("forms.customer-one.example", "app.acme.example:443"),
        LegacyDomain("Forms.Customer-Two.Example", "app.acme.example:443"),
        LegacyDomain("apex.example", "app.acme.example:443"),
    ]
    assert parse_legacy_config({}) == []
    assert parse_legacy_config(_legacy_config(), port=8443) == []


def test_import_without_grandfather_starts_in_pending_dns(session, make_application):
    acme = make_application("acme")
    report = import_legacy_domains(session, acme, parse_legacy_config(_legacy_config()))
    session.commit()
    assert [d.hostname for d in report.imported] == [
        "forms.customer-one.example",
        "forms.customer-two.example",
    ]
    assert report.skipped == [("apex.example", "apex_not_supported")]
    for domain in report.imported:
        assert domain.status == DomainStatus.PENDING_DNS
        assert domain.active_claim.status == ClaimStatus.PENDING
        assert domain.reference == domain.hostname
        assert domain.extra["legacy_upstream"] == "app.acme.example:443"
        assert EventType.DOMAIN_IMPORTED in [e.event_type for e in domain.events]


def test_import_with_grandfather_marks_ownership_and_uses_reference_map(session, make_application):
    acme = make_application("acme")
    entries = parse_legacy_config(_legacy_config())
    report = import_legacy_domains(
        session,
        acme,
        entries,
        references={"forms.customer-one.example": "ws_one"},
        grandfather=True,
    )
    session.commit()
    one, two = report.imported
    assert one.reference == "ws_one"
    assert two.reference == "forms.customer-two.example"
    for domain in (one, two):
        assert domain.status == DomainStatus.PROVISIONING
        claim = domain.active_claim
        assert claim.status == ClaimStatus.VERIFIED
        assert claim.verification_method == LEGACY_IMPORT_METHOD
        assert domain.check(CheckType.OWNERSHIP).status == CheckStatus.PASSING
        assert domain.check(CheckType.CERTIFICATE).status == CheckStatus.PENDING

    again = import_legacy_domains(session, acme, entries, grandfather=True)
    session.commit()
    assert again.imported == []
    assert sorted(h for h, _ in again.skipped) == [
        "apex.example",
        "forms.customer-one.example",
        "forms.customer-two.example",
    ]
