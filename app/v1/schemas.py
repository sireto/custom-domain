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


class SetupProvider(BaseModel):
    id: str = Field(
        description="Stable provider id, such as `cloudflare`, `godaddy` or `route53`; "
        "`unknown` when the provider was not recognized.",
        examples=["cloudflare"],
    )
    name: str = Field(description="Display name.", examples=["Cloudflare"])


SetupMethod = Literal["domain_connect", "guided", "manual"]


class DomainSetup(BaseModel):
    """How the customer will publish the DNS records, from the detected DNS provider."""

    method: SetupMethod | None = Field(
        description="`domain_connect`: the provider supports one-step setup through Domain "
        "Connect and the service can use it. `guided`: follow the provider's guide at "
        "`guide_url`. `manual`: the provider has no specific guide; `guide_url` is the "
        "generic one. Null until the service has looked up the hostname's DNS zone."
    )
    provider: SetupProvider | None = Field(
        description="The DNS provider hosting the zone, from its nameservers. Null until "
        "the zone has been looked up."
    )
    zone: str = Field(
        description="The DNS zone the records go in (the registrable domain until the zone "
        "has been looked up).",
        examples=["customer.example"],
    )
    host: str = Field(
        description="The hostname relative to `zone`: what most DNS providers want in "
        "the record's Name or Host field.",
        examples=["forms"],
    )
    guide_url: str | None = Field(
        description="Setup instructions for the provider. Null until the zone has been looked up."
    )
    detected_at: datetime | None = Field(
        description="When the zone was last looked up. Null until then."
    )


class DomainResource(BaseModel):
    id: uuid.UUID
    hostname: str = Field(description="Canonical hostname (lowercase, punycode).")
    reference: str
    status: DomainStatus
    dns_records: list[DnsRecord] = Field(
        description="Records the customer must publish. Empty once the domain is deleted."
    )
    setup: DomainSetup
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


class ConnectRequest(BaseModel):
    """Ask for a signed Domain Connect apply URL for one domain."""

    model_config = ConfigDict(extra="forbid")

    redirect_uri: str | None = Field(
        default=None,
        max_length=512,
        description="Where the DNS provider sends the customer when they are done: an https "
        "URL with a host, no user name and no fragment, at most 512 bytes. It ends up in the "
        "signed URL, so it is the application's responsibility; point it at a page of your "
        "own. The customer arrives there with your `state` (and `error=access_denied` if "
        "they declined).",
        examples=["https://app.example/settings/domains?done=1"],
    )
    state: str | None = Field(
        default=None,
        max_length=512,
        description="Opaque value returned to `redirect_uri` unchanged, at most 512 bytes "
        "without control characters.",
        examples=["c2Vzc2lvbi00Mg"],
    )

    @field_validator("redirect_uri")
    @classmethod
    def _https(cls, value: str | None) -> str | None:
        from app.services.domain_connect import redirect_uri_problem

        if value is not None and (problem := redirect_uri_problem(value)):
            raise ValueError(f"redirect_uri {problem}")
        return value

    @field_validator("state")
    @classmethod
    def _plain(cls, value: str | None) -> str | None:
        from app.services.domain_connect import state_problem

        if value is not None and (problem := state_problem(value)):
            raise ValueError(f"state {problem}")
        return value


class ConnectResource(BaseModel):
    url: str = Field(
        description="The signed apply URL at the customer's DNS provider. Send the customer "
        "there (a link or a redirect); they approve the two records in one step."
    )
    provider: SetupProvider | None = Field(description="The DNS provider the URL goes to.")
    expires_at: datetime = Field(
        description="Ask for a new URL after this time. The DNS records in it stay valid as "
        "long as the domain's records do."
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


def setup_for(domain: Domain) -> DomainSetup:
    from app.services.dns_setup import domain_setup

    setup = domain_setup(domain)
    return DomainSetup(
        method=setup.method,
        provider=(
            SetupProvider(id=setup.provider_id, name=setup.provider_name)
            if setup.provider_id is not None
            else None
        ),
        zone=setup.zone,
        host=setup.host,
        guide_url=setup.guide_url,
        detected_at=setup.detected_at,
    )


def domain_resource(domain: Domain) -> DomainResource:
    checks = sorted(domain.checks, key=lambda c: list(CheckType).index(c.check_type))
    return DomainResource(
        id=domain.id,
        hostname=domain.hostname,
        reference=domain.reference,
        status=domain.status,
        dns_records=dns_records_for(domain),
        setup=setup_for(domain),
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
