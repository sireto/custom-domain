"""Idempotent create requests.

A client that retries ``POST /v1/domains`` with the same ``Idempotency-Key``
gets the domain created by the first attempt instead of a duplicate error.
Keys are scoped to the application, fingerprinted against the request body,
and expire after ``IDEMPOTENCY_TTL``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Application, IdempotencyKey
from app.models.types import utcnow
from app.services.errors import ServiceError

IDEMPOTENCY_TTL = timedelta(hours=24)
MAX_KEY_LENGTH = 255


class IdempotencyKeyReused(ServiceError):
    code = "idempotency_key_reused"


class IdempotencyInProgress(ServiceError):
    code = "idempotency_request_in_progress"


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def begin(
    session: Session,
    application: Application,
    key: str,
    request_hash: str,
    *,
    now: datetime | None = None,
) -> tuple[IdempotencyKey, bool]:
    """Register ``key`` for this application, or return the existing record.

    Returns ``(record, created)``. When ``created`` is false the caller must
    compare ``request_hash`` and replay the original result.
    """
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ServiceError("Idempotency key must be 1-255 characters")
    now = now or utcnow()
    record = IdempotencyKey(
        application_id=application.id,
        key=key,
        request_hash=request_hash,
        created_at=now,
        expires_at=now + IDEMPOTENCY_TTL,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
        return record, True
    except IntegrityError:
        existing = session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.application_id == application.id, IdempotencyKey.key == key
            )
        )
        if existing is None:  # pragma: no cover - the row was purged in between
            raise
        return existing, False


def complete(session: Session, record: IdempotencyKey, domain_id: uuid.UUID) -> None:
    record.domain_id = domain_id
    session.flush()


def purge_expired(session: Session, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    result = session.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now))
    session.flush()
    return result.rowcount or 0
