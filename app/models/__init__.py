"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.application import ApiCredential, Application, VerifiedOrigin
from app.models.domain import Domain, DomainCheck, DomainEvent, OwnershipClaim
from app.models.edge import EdgeLock
from app.models.enums import (
    ApplicationStatus,
    CheckStatus,
    CheckType,
    ClaimStatus,
    DomainStatus,
    EventType,
    OriginStatus,
)
from app.models.idempotency import IdempotencyKey

__all__ = [
    "ApiCredential",
    "Application",
    "ApplicationStatus",
    "CheckStatus",
    "CheckType",
    "ClaimStatus",
    "Domain",
    "DomainCheck",
    "DomainEvent",
    "DomainStatus",
    "EdgeLock",
    "EventType",
    "IdempotencyKey",
    "OriginStatus",
    "OwnershipClaim",
    "VerifiedOrigin",
]
