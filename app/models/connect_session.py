"""Connect sessions: links to the setup page for one domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class ConnectSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A link to ``/connect/<token>`` for one domain (app/services/connect_sessions.py).

    Only the SHA-256 of the token is stored: the token appears once, in the
    response that created the session. A session lets whoever holds the link
    read the domain's setup and status, ask for a check and start a Domain
    Connect apply, and nothing else. It ends at ``expires_at`` or when it is
    revoked; the row stays until its domain is purged, so the history is kept.
    """

    __tablename__ = "connect_sessions"
    __table_args__ = (
        Index("ix_connect_sessions_domain_created", "domain_id", "created_at"),
        Index("ix_connect_sessions_application_created", "application_id", "created_at"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Shown on the page as "Back to <app_name>", linking to return_url.
    app_name: Mapped[str | None] = mapped_column(String(64))
    return_url: Mapped[str | None] = mapped_column(String(512))
