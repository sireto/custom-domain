from enum import StrEnum


class ApplicationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OriginStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    RETIRED = "retired"


class DomainStatus(StrEnum):
    PENDING_DNS = "pending_dns"
    PROVISIONING = "provisioning"
    READY = "ready"
    ATTENTION_REQUIRED = "attention_required"
    SUSPENDED = "suspended"
    DELETING = "deleting"


class ClaimStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REVOKED = "revoked"


class CheckType(StrEnum):
    OWNERSHIP = "ownership"
    ROUTING = "routing"
    CERTIFICATE = "certificate"
    ORIGIN = "origin"


class CheckStatus(StrEnum):
    PENDING = "pending"
    PASSING = "passing"
    FAILING = "failing"


class EventType(StrEnum):
    DOMAIN_CREATED = "domain.created"
    DOMAIN_IMPORTED = "domain.imported"
    CLAIM_ISSUED = "domain.claim_issued"
    CLAIM_VERIFIED = "domain.claim_verified"
    CLAIM_REVOKED = "domain.claim_revoked"
    CHECK_UPDATED = "domain.check_updated"
    STATUS_CHANGED = "domain.status_changed"
    DOMAIN_DELETED = "domain.deleted"
    RECHECK_REQUESTED = "domain.recheck_requested"
