from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CheckStatus, CheckType, ClaimStatus, DomainStatus
from app.models.types import (
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    enum_type,
    utcnow,
)

if TYPE_CHECKING:
    from app.models.application import Application


class Domain(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A customer hostname registered by one application for one workspace.

    Rows are never reused: deleting a domain leaves a tombstone (``deleted_at``)
    and a later claim of the same hostname creates a new row with fresh
    ownership material. The partial unique index enforces that only one live
    row per hostname exists across all applications, including under
    concurrent creates.
    """

    __tablename__ = "domains"
    __table_args__ = (
        Index(
            "ux_domains_hostname_live",
            "hostname",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_domains_application_status", "application_id", "status"),
        Index("ix_domains_application_reference", "application_id", "reference"),
        Index("ix_domains_purge_after", "purge_after"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    # Opaque workspace identifier owned by the application. Never interpreted.
    reference: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DomainStatus] = mapped_column(
        enum_type(DomainStatus, "domain_status"),
        nullable=False,
        default=DomainStatus.PENDING_DNS,
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)

    application: Mapped[Application] = relationship(back_populates="domains")
    claims: Mapped[list[OwnershipClaim]] = relationship(
        back_populates="domain",
        cascade="all, delete-orphan",
        order_by="OwnershipClaim.created_at",
    )
    checks: Mapped[list[DomainCheck]] = relationship(
        back_populates="domain", cascade="all, delete-orphan"
    )
    events: Mapped[list[DomainEvent]] = relationship(
        back_populates="domain",
        cascade="all, delete-orphan",
        order_by="DomainEvent.created_at",
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def active_claim(self) -> OwnershipClaim | None:
        return next((claim for claim in self.claims if claim.status != ClaimStatus.REVOKED), None)

    def check(self, check_type: CheckType) -> DomainCheck | None:
        return next((c for c in self.checks if c.check_type == check_type), None)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Domain {self.hostname} {self.status}>"


class OwnershipClaim(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The DNS challenge material a customer must publish to prove control.

    A domain has at most one claim that is not revoked. Reassigning a hostname
    or re-issuing instructions always creates a new claim with a new token, so
    stale verification can never be reused.
    """

    __tablename__ = "ownership_claims"
    __table_args__ = (
        Index(
            "ux_ownership_claims_one_live",
            "domain_id",
            unique=True,
            postgresql_where=text("status <> 'revoked'"),
            sqlite_where=text("status <> 'revoked'"),
        ),
    )

    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    txt_record_name: Mapped[str] = mapped_column(String(253), nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Snapshot of the application's CNAME target when the claim was issued.
    cname_target: Mapped[str] = mapped_column(String(253), nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(
        enum_type(ClaimStatus, "claim_status"), nullable=False, default=ClaimStatus.PENDING
    )
    # How ownership was established: 'dns_txt' or 'legacy_import'.
    verification_method: Mapped[str | None] = mapped_column(String(32))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    domain: Mapped[Domain] = relationship(back_populates="claims")

    @property
    def txt_record_value(self) -> str:
        return f"custom-domain-verify={self.token}"


class DomainCheck(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Current state of one lifecycle check. History lives in domain_events."""

    __tablename__ = "domain_checks"
    __table_args__ = (UniqueConstraint("domain_id", "check_type"),)

    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    check_type: Mapped[CheckType] = mapped_column(
        enum_type(CheckType, "check_type"), nullable=False
    )
    status: Mapped[CheckStatus] = mapped_column(
        enum_type(CheckStatus, "check_status"), nullable=False, default=CheckStatus.PENDING
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    domain: Mapped[Domain] = relationship(back_populates="checks")


class DomainEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only audit trail; also the source for webhook deliveries (#10)."""

    __tablename__ = "domain_events"
    __table_args__ = (
        Index("ix_domain_events_application_created", "application_id", "created_at"),
        Index("ix_domain_events_domain_created", "domain_id", "created_at"),
    )

    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    domain: Mapped[Domain] = relationship(back_populates="events")
