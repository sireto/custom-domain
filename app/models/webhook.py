from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.types import TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class WebhookSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An application's endpoint for lifecycle events.

    ``secret`` signs deliveries and is distinct from API credentials and the
    edge assertion key. ``previous_secret`` stays valid until
    ``previous_secret_expires_at`` so consumers can rotate without missing
    deliveries.
    """

    __tablename__ = "webhook_subscriptions"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_secret: Mapped[str | None] = mapped_column(String(128))
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class WebhookDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One event for one subscription, with its attempt history.

    The payload is snapshotted at enqueue time so purging tombstones (and
    their events) never changes or loses what was delivered.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "event_id"),
        Index("ix_webhook_deliveries_due", "next_attempt_at", "delivered_at"),
    )

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    domain_id: Mapped[uuid.UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    abandoned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)

    subscription: Mapped[WebhookSubscription] = relationship(back_populates="deliveries")

    @property
    def state(self) -> str:
        if self.delivered_at is not None:
            return "delivered"
        if self.abandoned_at is not None:
            return "abandoned"
        return "pending"
