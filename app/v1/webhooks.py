"""Webhook contract, documented in the OpenAPI ``webhooks`` section.

Delivery, signing and retries are implemented in #10; this module fixes the
payload shape and event types so applications can build consumers now.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.v1 import examples
from app.v1.schemas import WebhookEvent

webhooks = APIRouter()

SIGNATURE_NOTE = (
    "Deliveries carry `X-Custom-Domain-Signature: t=<unix time>,v1=<hex HMAC-SHA256>` "
    "computed over `<t>.<raw body>` with the application's webhook secret, which is "
    "distinct from its API credential. Reject deliveries older than five minutes and "
    "treat the event `id` as the deduplication key; events may arrive more than once "
    "and out of order."
)


@webhooks.post(
    "domain.ready",
    summary="The domain passed every check and is serving",
    description="Sent when a domain enters `ready`. " + SIGNATURE_NOTE,
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"example": examples.WEBHOOK_READY}},
            "required": True,
        }
    },
)
def domain_ready(event: WebhookEvent) -> None:  # pragma: no cover - documentation only
    """Documented webhook; never served by this API."""


@webhooks.post(
    "domain.attention_required",
    summary="A previously satisfied check is failing",
    description="Sent when a domain enters `attention_required`, for example on DNS drift. "
    + SIGNATURE_NOTE,
)
def domain_attention_required(event: WebhookEvent) -> None:  # pragma: no cover
    """Documented webhook; never served by this API."""


@webhooks.post(
    "domain.recovered",
    summary="The domain returned to ready after attention was required",
    description="Sent when a domain moves from `attention_required` back to `ready`. "
    + SIGNATURE_NOTE,
)
def domain_recovered(event: WebhookEvent) -> None:  # pragma: no cover
    """Documented webhook; never served by this API."""


@webhooks.post(
    "domain.deleted",
    summary="The domain was deleted and stopped serving",
    description="Sent when a domain is deleted. `dns_records` is empty. " + SIGNATURE_NOTE,
)
def domain_deleted(event: WebhookEvent) -> None:  # pragma: no cover
    """Documented webhook; never served by this API."""
