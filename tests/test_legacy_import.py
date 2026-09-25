from app.caddy import saas_template
from app.legacy import (
    LEGACY_IMPORT_METHOD,
    MISSING_REFERENCE,
    LegacyDomain,
    import_legacy_domains,
    parse_legacy_config,
)
from app.models import CheckStatus, CheckType, ClaimStatus, DomainStatus, EventType

FULL_MAP = {
    "forms.customer-one.example": "ws_one",
    "forms.customer-two.example": "ws_two",
}


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


def test_import_requires_a_reference_for_every_hostname(session, make_application):
    acme = make_application("acme")
    entries = parse_legacy_config(_legacy_config())

    report = import_legacy_domains(session, acme, entries)
    assert report.imported == []
    assert not report.complete
    assert report.skipped == [
        ("forms.customer-one.example", MISSING_REFERENCE),
        ("forms.customer-two.example", MISSING_REFERENCE),
        ("apex.example", "apex_not_supported"),
    ]

    partial = import_legacy_domains(
        session, acme, entries, references={"forms.customer-one.example": "ws_one"}
    )
    assert [d.hostname for d in partial.imported] == ["forms.customer-one.example"]
    assert partial.imported[0].reference == "ws_one"
    assert partial.skipped == [
        ("forms.customer-two.example", MISSING_REFERENCE),
        ("apex.example", "apex_not_supported"),
    ]
    session.rollback()


def test_hostname_as_reference_is_an_explicit_fallback(session, make_application):
    acme = make_application("acme")
    report = import_legacy_domains(
        session,
        acme,
        parse_legacy_config(_legacy_config()),
        references={"forms.customer-one.example": "ws_one"},
        hostname_as_reference=True,
    )
    session.commit()
    assert [(d.hostname, d.reference) for d in report.imported] == [
        ("forms.customer-one.example", "ws_one"),
        ("forms.customer-two.example", "forms.customer-two.example"),
    ]
    assert report.skipped == [("apex.example", "apex_not_supported")]


def test_import_without_grandfather_starts_in_pending_dns(session, make_application):
    acme = make_application("acme")
    report = import_legacy_domains(
        session, acme, parse_legacy_config(_legacy_config()), references=FULL_MAP
    )
    session.commit()
    assert [d.hostname for d in report.imported] == [
        "forms.customer-one.example",
        "forms.customer-two.example",
    ]
    assert report.skipped == [("apex.example", "apex_not_supported")]
    for domain in report.imported:
        assert domain.status == DomainStatus.PENDING_DNS
        assert domain.active_claim.status == ClaimStatus.PENDING
        assert domain.reference == FULL_MAP[domain.hostname]
        assert domain.extra["legacy_upstream"] == "app.acme.example:443"
        assert EventType.DOMAIN_IMPORTED in [e.event_type for e in domain.events]


def test_import_with_grandfather_marks_ownership_and_rerun_is_noop(session, make_application):
    acme = make_application("acme")
    entries = parse_legacy_config(_legacy_config())
    report = import_legacy_domains(session, acme, entries, references=FULL_MAP, grandfather=True)
    session.commit()
    one, two = report.imported
    assert (one.reference, two.reference) == ("ws_one", "ws_two")
    for domain in (one, two):
        assert domain.status == DomainStatus.PROVISIONING
        claim = domain.active_claim
        assert claim.status == ClaimStatus.VERIFIED
        assert claim.verification_method == LEGACY_IMPORT_METHOD
        assert domain.check(CheckType.OWNERSHIP).status == CheckStatus.PASSING
        assert domain.check(CheckType.CERTIFICATE).status == CheckStatus.PENDING

    again = import_legacy_domains(session, acme, entries, references=FULL_MAP, grandfather=True)
    session.commit()
    assert again.imported == []
    assert {d.id for d in again.existing} == {one.id, two.id}
    assert again.skipped == [("apex.example", "apex_not_supported")]


def test_hostname_owned_by_another_application_is_skipped(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    entries = [LegacyDomain("forms.customer-one.example", "app.acme.example:443")]
    import_legacy_domains(session, acme, entries, references=FULL_MAP)
    session.commit()

    report = import_legacy_domains(session, globex, entries, references=FULL_MAP)
    assert report.imported == [] and report.existing == []
    assert report.skipped == [("forms.customer-one.example", "hostname_already_claimed")]
