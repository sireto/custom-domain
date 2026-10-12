"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.application import ApiCredential, Application, AssertionKey, VerifiedOrigin
from app.models.domain import DnsZone, Domain, DomainCheck, DomainEvent, OwnershipClaim
from app.models.domain_connect import DomainConnectSigner
from app.models.edge import EdgeLock
from app.models.enums import (
    ApplicationStatus,
    CheckStatus,
    CheckType,
    ClaimStatus,
    CredentialScope,
    DomainStatus,
    EventType,
    OriginHostHeader,
    OriginStatus,
)
from app.models.idempotency import IdempotencyKey
from app.models.operator import OperatorToken
from app.models.traffic import ApplicationTraffic, EdgeTrafficCounter
from app.models.webhook import WebhookDelivery, WebhookSubscription

__all__ = [
    "ApiCredential",
    "Application",
    "ApplicationStatus",
    "ApplicationTraffic",
    "AssertionKey",
    "CheckStatus",
    "CheckType",
    "ClaimStatus",
    "CredentialScope",
    "DnsZone",
    "Domain",
    "DomainCheck",
    "DomainConnectSigner",
    "DomainEvent",
    "DomainStatus",
    "EdgeLock",
    "EdgeTrafficCounter",
    "EventType",
    "IdempotencyKey",
    "OperatorToken",
    "OriginHostHeader",
    "OriginStatus",
    "OwnershipClaim",
    "VerifiedOrigin",
    "WebhookDelivery",
    "WebhookSubscription",
]
