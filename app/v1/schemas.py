"""Request and response models for the v1 API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import CheckStatus, CheckType, ClaimStatus, Domain, DomainStatus

MAX_METADATA_KEYS = 32
MAX_METADATA_VALUE_LENGTH = 512

MetadataValue = str | int | float | bool | None

TXT_HELP = (
    "Create a TXT record with exactly this name and value. Many DNS providers "
    "expect only the part before your domain in the name field (for example "
    "`_custom-domain-challenge.forms`); do not add quotes and do not append the "
    "domain twice. Propagation can take up to the record's TTL."
)
CNAME_HELP = (
    "Point the hostname at the target with a CNAME record. Remove any A or AAAA "
    "record with the same name, do not proxy the record through a CDN until the "
    "domain is ready, and do not use a URL: the value is a hostname only."
)


class DomainCreate(BaseModel):
    """Register a customer hostname for a workspace of the calling application."""

    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(
        ...,
        min_length=1,
        max_length=253,
        description="Exact customer subdomain. Normalized to lowercase punycode.",
        examples=["forms.customer.example"],
    )
    reference: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Opaque workspace identifier owned by the application. Returned verbatim.",
        examples=["ws_8f3a1c"],
    )
    metadata: dict[str, MetadataValue] | None = Field(
        default=None,
        description="Optional caller metadata, at most 32 scalar values, stored and echoed back.",
        examples=[{"plan": "pro", "owner": "user_42"}],
    )

    @field_validator("metadata")
    @classmethod
    def _limit_metadata(cls, value: dict[str, MetadataValue] | None):
        if value is None:
            return value
        if len(value) > MAX_METADATA_KEYS:
            raise ValueError(f"metadata may hold at most {MAX_METADATA_KEYS} keys")
        for key, item in value.items():
            if len(key) > 64:
                raise ValueError("metadata keys may be at most 64 characters")
            if isinstance(item, str) and len(item) > MAX_METADATA_VALUE_LENGTH:
                raise ValueError(
                    f"metadata values may be at most {MAX_METADATA_VALUE_LENGTH} characters"
                )
        return value


class DnsRecord(BaseModel):
    name: str = Field(description="Fully qualified record name to create.")
    type: Literal["TXT", "CNAME"]
    value: str
    purpose: Literal["ownership", "routing"] = Field(
        description="`ownership` proves control of the hostname; "
        "`routing` sends traffic to the edge."
    )
    help: str = Field(description="Guidance for the customer's DNS console.")


class CheckResult(BaseModel):
    type: CheckType
    status: CheckStatus
    error_code: str | None = Field(
        default=None, description="Stable machine-readable failure code when `status` is `failing`."
    )
    message: str | None = None
    observed_at: datetime | None = None
    next_check_at: datetime | None = None


class DomainResource(BaseModel):
    id: uuid.UUID
    hostname: str = Field(description="Canonical hostname (lowercase, punycode).")
    reference: str
    status: DomainStatus
    dns_records: list[DnsRecord] = Field(
        description="Records the customer must publish. Empty once the domain is deleted."
    )
    checks: list[CheckResult]
    metadata: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class DomainPage(BaseModel):
    items: list[DomainResource]
    limit: int
    offset: int
    next_offset: int | None = Field(
        default=None, description="Pass as `offset` to fetch the next page; null on the last page."
    )


class ErrorBody(BaseModel):
    code: str = Field(examples=["hostname_already_claimed"])
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


WebhookEventType = Literal[
    "domain.ready", "domain.attention_required", "domain.recovered", "domain.deleted"
]


class WebhookEventData(BaseModel):
    domain: DomainResource


class WebhookEvent(BaseModel):
    """Payload delivered to an application's webhook endpoint (delivery is tracked in #10)."""

    id: uuid.UUID = Field(description="Stable event id; deliveries of the same event share it.")
    type: WebhookEventType
    created_at: datetime
    data: WebhookEventData


def dns_records_for(domain: Domain) -> list[DnsRecord]:
    claim = domain.active_claim
    if claim is None or claim.status == ClaimStatus.REVOKED:
        return []
    return [
        DnsRecord(
            name=claim.txt_record_name,
            type="TXT",
            value=claim.txt_record_value,
            purpose="ownership",
            help=TXT_HELP,
        ),
        DnsRecord(
            name=domain.hostname,
            type="CNAME",
            value=claim.cname_target,
            purpose="routing",
            help=CNAME_HELP,
        ),
    ]


def domain_resource(domain: Domain) -> DomainResource:
    checks = sorted(domain.checks, key=lambda c: list(CheckType).index(c.check_type))
    return DomainResource(
        id=domain.id,
        hostname=domain.hostname,
        reference=domain.reference,
        status=domain.status,
        dns_records=dns_records_for(domain),
        checks=[
            CheckResult(
                type=c.check_type,
                status=c.status,
                error_code=c.error_code,
                message=c.message,
                observed_at=c.observed_at,
                next_check_at=c.next_check_at,
            )
            for c in checks
        ],
        metadata=domain.extra,
        created_at=domain.created_at,
        updated_at=domain.updated_at,
        deleted_at=domain.deleted_at,
    )
