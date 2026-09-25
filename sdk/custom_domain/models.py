from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class DnsRecord:
    name: str
    type: str
    value: str
    purpose: str
    help: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DnsRecord:
        return cls(data["name"], data["type"], data["value"], data["purpose"], data.get("help", ""))

    def render(self) -> str:
        """One line suitable for a customer-facing DNS instruction."""
        return f"{self.type:5} {self.name}  ->  {self.value}"


@dataclass(frozen=True)
class Check:
    type: str
    status: str
    error_code: str | None
    message: str | None
    observed_at: datetime | None
    next_check_at: datetime | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Check:
        return cls(
            data["type"],
            data["status"],
            data.get("error_code"),
            data.get("message"),
            _dt(data.get("observed_at")),
            _dt(data.get("next_check_at")),
        )


@dataclass(frozen=True)
class Domain:
    id: str
    hostname: str
    reference: str
    status: str
    dns_records: list[DnsRecord]
    checks: list[Check]
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Domain:
        return cls(
            id=data["id"],
            hostname=data["hostname"],
            reference=data["reference"],
            status=data["status"],
            dns_records=[DnsRecord.from_dict(r) for r in data.get("dns_records", [])],
            checks=[Check.from_dict(c) for c in data.get("checks", [])],
            metadata=data.get("metadata"),
            created_at=_dt(data["created_at"]),
            updated_at=_dt(data["updated_at"]),
            deleted_at=_dt(data.get("deleted_at")),
        )

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    def check(self, check_type: str) -> Check | None:
        return next((c for c in self.checks if c.type == check_type), None)

    def render_dns_instructions(self) -> str:
        """Text to show a customer: the records to create and why."""
        lines = [f"Create these DNS records for {self.hostname}:", ""]
        for record in self.dns_records:
            lines.append(record.render())
            lines.append(f"      purpose: {record.purpose}. {record.help}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Page:
    items: list[Domain]
    limit: int
    offset: int
    next_offset: int | None


@dataclass(frozen=True)
class Webhook:
    id: str
    url: str
    events: list[str]
    active: bool
    created_at: datetime
    revoked_at: datetime | None
    previous_secret_expires_at: datetime | None
    secret: str | None = None  # only on create and rotate

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Webhook:
        return cls(
            id=data["id"],
            url=data["url"],
            events=list(data["events"]),
            active=data["active"],
            created_at=_dt(data["created_at"]),
            revoked_at=_dt(data.get("revoked_at")),
            previous_secret_expires_at=_dt(data.get("previous_secret_expires_at")),
            secret=data.get("secret"),
        )


@dataclass(frozen=True)
class Delivery:
    id: str
    event_id: str
    event_type: str
    domain_id: str | None
    state: str
    attempts: int
    next_attempt_at: datetime | None
    delivered_at: datetime | None
    last_status: int | None
    last_error: str | None
    created_at: datetime

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Delivery:
        return cls(
            id=data["id"],
            event_id=data["event_id"],
            event_type=data["event_type"],
            domain_id=data.get("domain_id"),
            state=data["state"],
            attempts=data["attempts"],
            next_attempt_at=_dt(data.get("next_attempt_at")),
            delivered_at=_dt(data.get("delivered_at")),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            created_at=_dt(data["created_at"]),
        )


@dataclass(frozen=True)
class WebhookEvent:
    id: str
    type: str
    created_at: datetime
    domain: Domain
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
