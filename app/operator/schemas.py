"""Request and response models for the operator API."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import ApiCredential, Application, VerifiedOrigin


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationCreate(_Strict):
    slug: str = Field(max_length=64, examples=["acme"])
    name: str = Field(max_length=200, examples=["Acme Forms"])
    cname_target: str | None = Field(
        default=None,
        max_length=253,
        description="The name customers CNAME to. Defaults to EDGE_HOSTNAME.",
        examples=["edge.example.net"],
    )


class ApplicationUpdate(_Strict):
    name: str | None = Field(default=None, max_length=200)
    cname_target: str | None = Field(default=None, max_length=253)
    reissue_claims: bool = Field(
        default=False,
        description="With a new cname_target: re-issue the DNS records of every live domain "
        "still on the old one (they wait for DNS until their customers publish them).",
    )
    workspace_probe: bool | None = Field(
        default=None, description="Whether readiness requires the origin's workspace check."
    )
    status: Literal["active", "suspended"] | None = None
    rate_limit_per_minute: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description="Proxied requests per minute across the application's hostnames, at the "
        "edge; null lifts it, omit to leave it unchanged.",
    )
    rate_limit_per_second: int | None = Field(
        default=None, ge=1, le=1_000_000, description="The same, per second."
    )
    max_domains: int | None = Field(
        default=None,
        ge=1,
        description="At most this many live domains; null removes the limit. Omit to leave "
        "it unchanged. Lowering it below the current count keeps existing domains working "
        "and refuses new ones with domain_limit_reached.",
    )


class ApplicationResource(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    cname_target: str
    workspace_probe: bool
    max_domains: int | None = Field(description="The application's limit; null for none.")
    live_domains: int = Field(description="Registered and not deleted domains.")
    rate_limit_per_minute: int | None = Field(description="null for no limit")
    rate_limit_per_second: int | None = Field(description="null for no limit")
    created_at: datetime

    @classmethod
    def of(cls, application: Application, live_domains: int) -> ApplicationResource:
        return cls(
            id=application.id,
            slug=application.slug,
            name=application.name,
            status=application.status.value,
            cname_target=application.cname_target,
            workspace_probe=application.workspace_probe_enabled,
            max_domains=application.max_domains,
            live_domains=live_domains,
            rate_limit_per_minute=application.rate_limit_per_minute,
            rate_limit_per_second=application.rate_limit_per_second,
            created_at=application.created_at,
        )


class TrafficDayResource(BaseModel):
    date: date_type = Field(description="A UTC day.")
    requests: int = Field(description="Proxied requests, including ones the edge refused.")
    response_bytes: int = Field(description="Response body bytes sent to clients.")


class ApplicationTrafficResource(BaseModel):
    application: str
    days: list[TrafficDayResource] = Field(description="Oldest first; today is last and partial.")
    requests: int = Field(description="Total over the days listed.")
    response_bytes: int = Field(description="Total over the days listed.")


class ApplicationDeleted(BaseModel):
    deleted: str = Field(description="The slug the application had.")
    live_domains_deleted: int


class OriginCreate(_Strict):
    host: str = Field(max_length=253, examples=["app.acme.example"])
    scheme: Literal["https", "http"] = "https"
    port: int | None = Field(default=None, ge=1, le=65535)


class OriginVerify(_Strict):
    activate: bool = Field(default=True, description="Activate the origin once verified.")


class OriginResource(BaseModel):
    id: uuid.UUID
    url: str
    status: str
    active: bool
    verification_token: str | None = Field(
        description="Serve this as the plain-text body of verification_url, then verify."
    )
    verification_url: str
    verified_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None

    @classmethod
    def of(cls, origin: VerifiedOrigin) -> OriginResource:
        from app.services.origin_verification import WELL_KNOWN_PATH

        return cls(
            id=origin.id,
            url=origin.url,
            status=origin.status.value,
            active=origin.is_active,
            verification_token=origin.verification_token,
            verification_url=f"{origin.url}{WELL_KNOWN_PATH}",
            verified_at=origin.verified_at,
            last_error_code=origin.last_error_code,
            last_error_message=origin.last_error_message,
        )


class CredentialCreate(_Strict):
    label: str = Field(max_length=100, examples=["backend"])
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class CredentialRotate(_Strict):
    grace_hours: int = Field(default=24, ge=0, le=720)


class CredentialResource(BaseModel):
    id: uuid.UUID
    label: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def of(cls, credential: ApiCredential) -> CredentialResource:
        return cls(
            id=credential.id,
            label=credential.label,
            key_prefix=credential.key_prefix,
            created_at=credential.created_at,
            last_used_at=credential.last_used_at,
            expires_at=credential.expires_at,
            revoked_at=credential.revoked_at,
        )


class NewCredential(CredentialResource):
    secret: str = Field(description="The API key. Shown once; only its hash is stored.")


class AssertionKeyCreate(_Strict):
    activate_in_hours: float = Field(
        default=24,
        ge=0,
        le=720,
        description="When the key starts signing. Add it to the origin's keyring before then; "
        "0 signs at once, which suits an application whose origin has no key yet.",
    )


class AssertionKeyResource(BaseModel):
    key_id: str = Field(description="The id in the assertion: v1.<key id>.<payload>.<mac>.")
    state: Literal["signing", "next", "previous", "revoked"]
    active_from: datetime
    created_at: datetime
    revoked_at: datetime | None


class AssertionKeys(BaseModel):
    application_id: uuid.UUID = Field(
        description="The assertion's `app`: the id the origin compares with its own."
    )
    signing: str | None = Field(
        description="The key id signing now, or null while the deployment key signs."
    )
    keys: list[AssertionKeyResource]


class NewAssertionKey(AssertionKeyResource):
    application_id: uuid.UUID
    secret: str = Field(description="The signing secret. Shown once; give it to the origin.")


class Finding(BaseModel):
    check: str
    status: Literal["ok", "warn", "fail"]
    detail: str


class OperatorTokenSet(_Strict):
    token: str = Field(
        min_length=32,
        max_length=256,
        description="The new token: 32 to 256 letters, digits and . _ ~ + / = -",
    )


class DoctorReport(BaseModel):
    ok: int
    warn: int
    fail: int
    findings: list[Finding]
