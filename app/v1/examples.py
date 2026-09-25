"""Worked examples embedded in the OpenAPI document and docs/api-v1.md.

They follow one domain through registration, waiting for DNS, readiness, DNS
drift and deletion. Hostnames and identifiers are invented.
"""

from __future__ import annotations

import copy
from typing import Any

DOMAIN_ID = "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f"
TXT_NAME = "_custom-domain-challenge.forms.customer.example"
TXT_VALUE = "custom-domain-verify=Qm9vayBvZiB0aGUgZGVhZCwgY2hhcHRlciBzZXZlbg"
CNAME_TARGET = "acme.edge.example.net"

DNS_RECORDS = [
    {
        "name": TXT_NAME,
        "type": "TXT",
        "value": TXT_VALUE,
        "purpose": "ownership",
        "help": "Create a TXT record with exactly this name and value.",
    },
    {
        "name": "forms.customer.example",
        "type": "CNAME",
        "value": CNAME_TARGET,
        "purpose": "routing",
        "help": "Point the hostname at the target with a CNAME record.",
    },
]


def _checks(**overrides: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for check_type in ("ownership", "routing", "certificate", "origin"):
        base = {
            "type": check_type,
            "status": "pending",
            "error_code": None,
            "message": None,
            "observed_at": None,
            "next_check_at": None,
        }
        base.update(overrides.get(check_type, {}))
        checks.append(base)
    return checks


def _domain(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": DOMAIN_ID,
        "hostname": "forms.customer.example",
        "reference": "ws_8f3a1c",
        "status": "pending_dns",
        "dns_records": copy.deepcopy(DNS_RECORDS),
        "checks": _checks(),
        "metadata": {"plan": "pro"},
        "created_at": "2026-09-25T14:00:00Z",
        "updated_at": "2026-09-25T14:00:00Z",
        "deleted_at": None,
    }
    base.update(overrides)
    return base


REGISTRATION = _domain()

WAITING_FOR_DNS = _domain(
    updated_at="2026-09-25T14:05:00Z",
    checks=_checks(
        ownership={
            "status": "failing",
            "error_code": "txt_record_not_found",
            "message": f"No TXT record named {TXT_NAME} was found",
            "observed_at": "2026-09-25T14:05:00Z",
            "next_check_at": "2026-09-25T14:10:00Z",
        },
        routing={
            "status": "failing",
            "error_code": "cname_not_found",
            "message": "forms.customer.example has no CNAME record",
            "observed_at": "2026-09-25T14:05:00Z",
            "next_check_at": "2026-09-25T14:10:00Z",
        },
    ),
)

_PASSING = {"status": "passing", "observed_at": "2026-09-25T15:00:00Z"}
READY = _domain(
    status="ready",
    updated_at="2026-09-25T15:00:00Z",
    checks=_checks(ownership=_PASSING, routing=_PASSING, certificate=_PASSING, origin=_PASSING),
)

DNS_DRIFT = _domain(
    status="attention_required",
    updated_at="2026-10-02T09:30:00Z",
    checks=_checks(
        ownership=_PASSING,
        routing={
            "status": "failing",
            "error_code": "cname_target_mismatch",
            "message": f"CNAME points to old-host.example, expected {CNAME_TARGET}",
            "observed_at": "2026-10-02T09:30:00Z",
            "next_check_at": "2026-10-02T09:45:00Z",
        },
        certificate=_PASSING,
        origin=_PASSING,
    ),
)

DELETED = _domain(
    status="deleting",
    dns_records=[],
    updated_at="2026-10-10T08:00:00Z",
    deleted_at="2026-10-10T08:00:00Z",
    checks=_checks(ownership=_PASSING, routing=_PASSING, certificate=_PASSING, origin=_PASSING),
)

CREATE_REQUEST = {
    "hostname": "Forms.Customer.Example",
    "reference": "ws_8f3a1c",
    "metadata": {"plan": "pro"},
}

ERRORS = {
    "unauthorized": {
        "error": {
            "code": "unauthorized",
            "message": "A valid application credential is required",
            "details": {},
        }
    },
    "hostname_already_claimed": {
        "error": {
            "code": "hostname_already_claimed",
            "message": "forms.customer.example is already claimed",
            "details": {},
        }
    },
    "apex_not_supported": {
        "error": {
            "code": "apex_not_supported",
            "message": "Only subdomains such as forms.example.com are supported; "
            "apex domains are not supported yet",
            "details": {"field": "hostname"},
        }
    },
    "domain_not_found": {
        "error": {"code": "domain_not_found", "message": "domain_not_found", "details": {}}
    },
    "idempotency_key_reused": {
        "error": {
            "code": "idempotency_key_reused",
            "message": "Idempotency-Key was already used with a different request body",
            "details": {},
        }
    },
}

WEBHOOK_READY = {
    "id": "b7e2c1a0-9d8f-4e7a-b6c5-d4e3f2a1b0c9",
    "type": "domain.ready",
    "created_at": "2026-09-25T15:00:00Z",
    "data": {"domain": READY},
}


def example(name: str, value: dict[str, Any], summary: str) -> dict[str, Any]:
    return {name: {"summary": summary, "value": value}}
