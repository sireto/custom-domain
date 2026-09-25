import pytest
from sqlalchemy import func, select

from app.models import OriginStatus, VerifiedOrigin
from app.services.applications import (
    activate_origin,
    get_active_origin,
    record_origin_verification,
    register_origin,
    retire_origin,
)
from app.services.errors import InvalidOrigin, OriginConflict, OriginNotVerified


def test_register_creates_pending_origin_with_token(session, make_application):
    acme = make_application("acme")
    origin = register_origin(session, acme, host="App.Acme.Example")
    session.commit()
    assert origin.status == OriginStatus.PENDING
    assert origin.scheme == "https"
    assert origin.port == 443
    assert origin.host == "app.acme.example"
    assert origin.url == "https://app.acme.example:443"
    assert origin.verification_token and len(origin.verification_token) >= 30
    assert not origin.is_active


def test_register_validates_input_and_uniqueness(session, make_application):
    acme = make_application("acme")
    register_origin(session, acme, host="app.acme.example")
    session.commit()
    with pytest.raises(OriginConflict):
        register_origin(session, acme, host="app.acme.example")
    with pytest.raises(InvalidOrigin):
        register_origin(session, acme, host="app.acme.example", scheme="ftp")
    with pytest.raises(InvalidOrigin):
        register_origin(session, acme, host="10.0.0.1")
    with pytest.raises(InvalidOrigin):
        register_origin(session, acme, host="app.acme.example", port=70000)
    # Same host is fine for another application.
    globex = make_application("globex")
    assert register_origin(session, globex, host="app.acme.example").application_id == globex.id


def test_only_verified_origin_can_be_active_and_only_one(session, make_application):
    acme = make_application("acme")
    first = register_origin(session, acme, host="app.acme.example")
    with pytest.raises(OriginNotVerified):
        activate_origin(session, first)

    record_origin_verification(session, first, verified=True)
    activate_origin(session, first)
    session.commit()
    assert get_active_origin(session, acme).id == first.id

    second = register_origin(session, acme, host="app2.acme.example", port=8443)
    record_origin_verification(
        session, second, verified=False, error_code="tls_failed", message="x"
    )
    assert second.status == OriginStatus.FAILED
    with pytest.raises(OriginNotVerified):
        activate_origin(session, second)
    record_origin_verification(session, second, verified=True)
    activate_origin(session, second)
    session.commit()

    active_count = session.scalar(
        select(func.count())
        .select_from(VerifiedOrigin)
        .where(VerifiedOrigin.application_id == acme.id, VerifiedOrigin.is_active.is_(True))
    )
    assert active_count == 1
    assert get_active_origin(session, acme).id == second.id
    assert session.get(VerifiedOrigin, first.id).is_active is False

    retire_origin(session, second)
    session.commit()
    assert get_active_origin(session, acme) is None
