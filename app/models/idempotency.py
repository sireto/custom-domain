from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime, UUIDPrimaryKeyMixin, utcnow


class IdempotencyKey(UUIDPrimaryKeyMixin, Base):
    """Remembers the outcome of a create request so a retry returns the same domain.

    Scoped to the application that sent the key. ``request_hash`` fingerprints
    the request body so a reused key with a different body is rejected.
    ``domain_id`` is null while the original request is still in flight and
    after the domain has been purged.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("application_id", "key"),)

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
