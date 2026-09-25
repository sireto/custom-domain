import uuid

import pytest

from app.models import DomainStatus
from app.services.applications import (
    authenticate_credential,
    issue_credential,
    revoke_credential,
)
from app.services.domains import (
    claim_domain,
    delete_domain,
    get_domain,
    list_domains,
    list_events,
    reissue_claim,
)
from app.services.errors import CredentialNotFound, DomainNotFound


@pytest.fixture
def two_tenants(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    a1 = claim_domain(session, acme, "one.acme-customer.example", "ws_a1")
    a2 = claim_domain(session, acme, "two.acme-customer.example", "ws_a2")
    g1 = claim_domain(session, globex, "one.globex-customer.example", "ws_g1")
    session.commit()
    return acme, globex, a1, a2, g1


def test_get_domain_is_application_scoped(session, two_tenants):
    acme, globex, a1, _, g1 = two_tenants
    assert get_domain(session, acme, a1.id).id == a1.id
    with pytest.raises(DomainNotFound):
        get_domain(session, globex, a1.id)
    with pytest.raises(DomainNotFound):
        get_domain(session, acme, g1.id)
    with pytest.raises(DomainNotFound):
        get_domain(session, acme, uuid.uuid4())


def test_list_domains_only_returns_own_rows(session, two_tenants):
    acme, globex, a1, a2, g1 = two_tenants
    assert {d.id for d in list_domains(session, acme)} == {a1.id, a2.id}
    assert {d.id for d in list_domains(session, globex)} == {g1.id}
    assert [d.id for d in list_domains(session, acme, reference="ws_a2")] == [a2.id]
    assert list_domains(session, acme, reference="ws_g1") == []
    assert {d.id for d in list_domains(session, acme, status=DomainStatus.PENDING_DNS)} == {
        a1.id,
        a2.id,
    }
    assert list_domains(session, acme, status=DomainStatus.READY) == []


def test_list_domains_pagination_is_stable(session, two_tenants):
    acme, _, a1, a2, _ = two_tenants
    first = list_domains(session, acme, limit=1, offset=0)
    second = list_domains(session, acme, limit=1, offset=1)
    assert [first[0].id, second[0].id] == [a1.id, a2.id]
    assert list_domains(session, acme, limit=1, offset=5) == []


def test_delete_and_reissue_are_application_scoped(session, two_tenants):
    acme, globex, a1, _, _ = two_tenants
    with pytest.raises(DomainNotFound):
        delete_domain(session, globex, a1.id)
    with pytest.raises(DomainNotFound):
        reissue_claim(session, globex, a1.id)
    with pytest.raises(DomainNotFound):
        list_events(session, globex, a1.id)
    assert get_domain(session, acme, a1.id).deleted_at is None


def test_events_are_tagged_with_owning_application(session, two_tenants):
    acme, _, a1, _, _ = two_tenants
    events = list_events(session, acme, a1.id)
    assert events and all(e.application_id == acme.id for e in events)


def test_credential_resolves_only_its_application(session, two_tenants):
    acme, globex, *_ = two_tenants
    _, acme_secret = issue_credential(session, acme, label="ci")
    _, globex_secret = issue_credential(session, globex, label="ci")
    session.commit()

    assert authenticate_credential(session, acme_secret).application.id == acme.id
    assert authenticate_credential(session, globex_secret).application.id == globex.id


def test_credential_revocation_is_application_scoped(session, two_tenants):
    acme, globex, *_ = two_tenants
    credential, _ = issue_credential(session, acme, label="ci")
    session.commit()
    with pytest.raises(CredentialNotFound):
        revoke_credential(session, globex, credential.id)
    assert revoke_credential(session, acme, credential.id).revoked_at is not None
