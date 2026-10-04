"""Per-application keys for the edge's signed assertion.

Without one, an application's requests are signed with the deployment's
``EDGE_ASSERTION_KEYS``, which every origin on the deployment must hold. On a
deployment shared by several parties, a key per application means one
application's origin can't be sent assertions forged with another's key.

Issuing a key never breaks a working origin: the new key starts signing at
``active_from`` (24 hours from now by default). Until then the previous key,
or the deployment key, keeps signing, so the origin can add the new key to
its keyring first. Issuing also revokes every key the previous signing key
had already replaced, so an application holds at most two live keys: the
one signing now and the next one. Revoking the signing key falls back to the
previous one, or to the deployment key.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, AssertionKey
from app.models.types import utcnow
from app.services.errors import AssertionKeyNotFound, InvalidAssertionKey

KEY_ID_PREFIX = "app_"
DEFAULT_ACTIVATION = timedelta(hours=24)
MAX_ACTIVATION = timedelta(days=30)


def _live(session: Session, application: Application) -> list[AssertionKey]:
    """Keys not revoked, newest activation first."""
    return list(
        session.scalars(
            select(AssertionKey)
            .where(
                AssertionKey.application_id == application.id,
                AssertionKey.revoked_at.is_(None),
            )
            .order_by(AssertionKey.active_from.desc(), AssertionKey.created_at.desc())
        )
    )


def signing_key(
    session: Session, application_id: uuid.UUID, *, now: datetime | None = None
) -> AssertionKey | None:
    """The key the edge signs this application's requests with now, if it has one."""
    now = now or utcnow()
    return session.scalar(
        select(AssertionKey)
        .where(
            AssertionKey.application_id == application_id,
            AssertionKey.revoked_at.is_(None),
            AssertionKey.active_from <= now,
        )
        .order_by(AssertionKey.active_from.desc(), AssertionKey.created_at.desc())
        .limit(1)
    )


def list_keys(session: Session, application: Application) -> list[AssertionKey]:
    return list(
        session.scalars(
            select(AssertionKey)
            .where(AssertionKey.application_id == application.id)
            .order_by(AssertionKey.created_at.desc())
        )
    )


def issue_key(
    session: Session,
    application: Application,
    *,
    activate_in: timedelta = DEFAULT_ACTIVATION,
    now: datetime | None = None,
) -> tuple[AssertionKey, str]:
    """A new key, signing from ``now + activate_in``; returns it with its secret (shown once).

    Its id starts with ``app_``, which deployment key ids may not (EdgeSettings).
    """
    if not timedelta(0) <= activate_in <= MAX_ACTIVATION:
        raise InvalidAssertionKey(
            f"Activation must be between now and {MAX_ACTIVATION.days} days from now"
        )
    now = now or utcnow()
    current = signing_key(session, application.id, now=now)
    for key in _live(session, application):
        # Keep the key signing now; anything else live is replaced by the new one.
        if current is None or key.id != current.id:
            key.revoked_at = now
    key_id = KEY_ID_PREFIX + secrets.token_hex(8)
    secret = secrets.token_urlsafe(48)
    key = AssertionKey(
        application_id=application.id,
        key_id=key_id,
        secret=secret,
        active_from=now + activate_in,
    )
    session.add(key)
    session.flush()
    return key, secret


def revoke_key(
    session: Session, application: Application, key_id: str, *, now: datetime | None = None
) -> AssertionKey:
    """Stop signing and accepting a key. Another application's key is not found."""
    key = session.scalar(
        select(AssertionKey).where(
            AssertionKey.application_id == application.id, AssertionKey.key_id == key_id
        )
    )
    if key is None:
        raise AssertionKeyNotFound(f"No assertion key {key_id!r}")
    if key.revoked_at is None:
        key.revoked_at = now or utcnow()
        session.flush()
    return key


def key_state(key: AssertionKey, signing: AssertionKey | None, now: datetime) -> str:
    """``signing``, ``next`` (waiting for active_from), ``previous`` or ``revoked``."""
    if key.revoked_at is not None:
        return "revoked"
    if signing is not None and key.id == signing.id:
        return "signing"
    if key.active_from > now:
        return "next"
    return "previous"
