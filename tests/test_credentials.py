from datetime import timedelta

import pytest

from app.models import ApiCredential, ApplicationStatus
from app.models.types import utcnow
from app.services.applications import (
    KEY_PREFIX_LENGTH,
    SECRET_PREFIX,
    authenticate_credential,
    create_application,
    hash_secret,
    issue_credential,
    list_credentials,
    revoke_credential,
    set_application_status,
)
from app.services.errors import ApplicationAlreadyExists, InvalidApplication, InvalidCredential


def test_issue_returns_plaintext_once_and_stores_hash(session, make_application):
    acme = make_application("acme")
    credential, secret = issue_credential(session, acme, label="backend")
    session.commit()

    assert secret.startswith(SECRET_PREFIX)
    assert len(secret) > 40
    assert credential.key_prefix == secret[:KEY_PREFIX_LENGTH]
    assert credential.key_hash == hash_secret(secret)
    assert secret not in credential.key_hash
    stored = session.get(ApiCredential, credential.id)
    assert stored.key_hash != secret


def test_authenticate_resolves_application_and_records_use(session, make_application):
    acme = make_application("acme")
    credential, secret = issue_credential(session, acme, label="backend")
    session.commit()
    now = utcnow()
    resolved = authenticate_credential(session, secret, now=now)
    assert resolved.id == credential.id
    assert resolved.application.slug == "acme"
    assert resolved.last_used_at == now


@pytest.mark.parametrize("bad", [None, "", "cd_", "nope", "cd_" + "x" * 43])
def test_authenticate_rejects_unknown_secrets(session, make_application, bad):
    make_application("acme")
    with pytest.raises(InvalidCredential):
        authenticate_credential(session, bad)


def test_revoked_expired_and_suspended_credentials_fail(session, make_application):
    acme = make_application("acme")
    revoked, revoked_secret = issue_credential(session, acme, label="old")
    revoke_credential(session, acme, revoked.id)
    _, expired_secret = issue_credential(
        session, acme, label="short", expires_at=utcnow() - timedelta(seconds=1)
    )
    _, fine_secret = issue_credential(session, acme, label="fine")
    session.commit()

    with pytest.raises(InvalidCredential):
        authenticate_credential(session, revoked_secret)
    with pytest.raises(InvalidCredential):
        authenticate_credential(session, expired_secret)
    assert authenticate_credential(session, fine_secret).label == "fine"

    set_application_status(session, acme, ApplicationStatus.SUSPENDED)
    session.commit()
    with pytest.raises(InvalidCredential):
        authenticate_credential(session, fine_secret)


def test_list_credentials_never_exposes_secret(session, make_application):
    acme = make_application("acme")
    _, secret = issue_credential(session, acme, label="a")
    issue_credential(session, acme, label="b")
    session.commit()
    rows = list_credentials(session, acme)
    assert [r.label for r in rows] == ["a", "b"]
    assert all(
        secret not in (r.key_hash + r.key_prefix) or r.key_prefix == secret[:11] for r in rows
    )
    assert not hasattr(rows[0], "secret")


def test_create_application_validates_and_is_unique(session, make_application):
    make_application("acme")
    with pytest.raises(ApplicationAlreadyExists):
        create_application(session, slug="acme", name="Again", cname_target="x.edge.example.net")
    with pytest.raises(InvalidApplication):
        create_application(session, slug="Bad Slug", name="x", cname_target="x.edge.example.net")
    with pytest.raises(InvalidApplication):
        create_application(session, slug="ok", name="x", cname_target="*.edge.example.net")
    created = create_application(session, slug="ok", name="x", cname_target="Edge.Example.NET.")
    assert created.cname_target == "edge.example.net"
