from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ApplicationStatus, OriginStatus
from app.models.types import TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from app.models.domain import Domain


class Application(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A SaaS product integrating with the service. The tenant boundary."""

    __tablename__ = "applications"

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ApplicationStatus] = mapped_column(
        enum_type(ApplicationStatus, "application_status"),
        nullable=False,
        default=ApplicationStatus.ACTIVE,
    )
    # Hostname customers point their CNAME at. One per application so the
    # instructions and the routing lookup are both application specific (#6).
    cname_target: Mapped[str] = mapped_column(String(253), nullable=False)

    origins: Mapped[list[VerifiedOrigin]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    credentials: Mapped[list[ApiCredential]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    domains: Mapped[list[Domain]] = relationship(back_populates="application")

    @property
    def active_origin(self) -> VerifiedOrigin | None:
        return next((origin for origin in self.origins if origin.is_active), None)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Application {self.slug} {self.status}>"


class VerifiedOrigin(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Where an application's traffic is proxied. Only a verified origin can be active."""

    __tablename__ = "verified_origins"
    __table_args__ = (
        UniqueConstraint("application_id", "scheme", "host", "port"),
        CheckConstraint("scheme IN ('https', 'http')", name="scheme"),
        CheckConstraint("port > 0 AND port < 65536", name="port_range"),
        Index(
            "ux_verified_origins_one_active",
            "application_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    scheme: Mapped[str] = mapped_column(String(8), nullable=False, default="https")
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    status: Mapped[OriginStatus] = mapped_column(
        enum_type(OriginStatus, "origin_status"), nullable=False, default=OriginStatus.PENDING
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Proof-of-control token the operator publishes at the origin (#5).
    verification_token: Mapped[str | None] = mapped_column(String(128))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="origins")

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


class ApiCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Hashed application-scoped API secret. The plaintext is shown once at issue time."""

    __tablename__ = "api_credentials"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    # Non-secret identifier for support and audit logs.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    application: Mapped[Application] = relationship(back_populates="credentials")

    def is_usable(self, now: datetime) -> bool:
        not_revoked = self.revoked_at is None
        not_expired = self.expires_at is None or self.expires_at > now
        return not_revoked and not_expired
