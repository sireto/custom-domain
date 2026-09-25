from datetime import timedelta

from app.models.types import utcnow
from app.services import idempotency


def test_begin_reclaims_expired_keys_on_read(session, make_application):
    acme = make_application("acme")
    t0 = utcnow()
    first, created = idempotency.begin(session, acme, "k", "hash-a", now=t0)
    idempotency.complete(session, first, None)
    session.commit()
    assert created

    same, created = idempotency.begin(session, acme, "k", "hash-b", now=t0 + timedelta(hours=1))
    assert not created and same.id == first.id and same.request_hash == "hash-a"

    reclaimed, created = idempotency.begin(
        session, acme, "k", "hash-b", now=t0 + idempotency.IDEMPOTENCY_TTL + timedelta(seconds=1)
    )
    session.commit()
    assert created
    assert reclaimed.id == first.id
    assert reclaimed.request_hash == "hash-b"
    assert reclaimed.domain_id is None
    assert reclaimed.expires_at > t0 + idempotency.IDEMPOTENCY_TTL


def test_keys_are_scoped_per_application(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    _, created_a = idempotency.begin(session, acme, "shared", "h")
    _, created_b = idempotency.begin(session, globex, "shared", "h")
    assert created_a and created_b
