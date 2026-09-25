"""v1 webhook subscription endpoints. Contract: docs/webhooks.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.services import webhooks as service
from app.v1.deps import CurrentApplication, DbSession
from app.v1.schemas import ErrorResponse, WebhookEventType

router = APIRouter(
    prefix="/v1/webhooks", tags=["Webhooks"], responses={401: {"model": ErrorResponse}}
)


class WebhookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "Absolute https URL that receives POST deliveries. Must resolve to a public address."
        ),
        examples=["https://app.acme.example/hooks/custom-domain"],
    )
    events: list[WebhookEventType] = Field(
        min_length=1,
        description="Event types to deliver.",
        examples=[["domain.ready", "domain.deleted"]],
    )


class WebhookResource(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime
    revoked_at: datetime | None = None
    previous_secret_expires_at: datetime | None = Field(
        default=None, description="Until when the previous secret still signs, after a rotation."
    )


class WebhookCreated(WebhookResource):
    secret: str = Field(description="Signing secret, shown once. Distinct from API credentials.")


class DeliveryResource(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    domain_id: uuid.UUID | None
    state: Literal["pending", "delivered", "abandoned"]
    attempts: int
    next_attempt_at: datetime | None
    delivered_at: datetime | None
    abandoned_at: datetime | None
    last_attempt_at: datetime | None
    last_status: int | None
    last_error: str | None
    created_at: datetime


class ReplayResult(BaseModel):
    requeued: int


def allow_private_from_env() -> bool:
    import os

    return os.environ.get("ORIGIN_ALLOW_PRIVATE", "").strip().lower() in {"1", "true", "yes", "on"}


def _resource(subscription) -> WebhookResource:
    return WebhookResource(
        id=subscription.id,
        url=subscription.url,
        events=list(subscription.events),
        active=subscription.is_active,
        created_at=subscription.created_at,
        revoked_at=subscription.revoked_at,
        previous_secret_expires_at=subscription.previous_secret_expires_at,
    )


def _delivery(delivery) -> DeliveryResource:
    return DeliveryResource(
        id=delivery.id,
        event_id=delivery.event_id,
        event_type=delivery.event_type,
        domain_id=delivery.domain_id,
        state=delivery.state,
        attempts=delivery.attempts,
        next_attempt_at=delivery.next_attempt_at,
        delivered_at=delivery.delivered_at,
        abandoned_at=delivery.abandoned_at,
        last_attempt_at=delivery.last_attempt_at,
        last_status=delivery.last_status,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=WebhookCreated,
    summary="Subscribe to domain events",
    description=(
        "Registers an endpoint for the calling application. The signing secret is returned "
        "once; deliveries carry `X-Custom-Domain-Signature: t=<unix>,v1=<hex HMAC-SHA256>` over "
        "`<t>.<body>`. The URL must be https and resolve to a public address."
    ),
    responses={422: {"model": ErrorResponse}},
)
def create_webhook(
    payload: WebhookCreate, application: CurrentApplication, db: DbSession
) -> WebhookCreated:
    subscription, secret = service.create_subscription(
        db,
        application,
        url=payload.url,
        events=list(payload.events),
        allow_private=allow_private_from_env(),
    )
    db.commit()
    return WebhookCreated(**_resource(subscription).model_dump(), secret=secret)


@router.get("", response_model=list[WebhookResource], summary="List webhooks")
def list_webhooks(application: CurrentApplication, db: DbSession) -> list[WebhookResource]:
    return [_resource(s) for s in service.list_subscriptions(db, application)]


@router.delete(
    "/{webhook_id}",
    response_model=WebhookResource,
    summary="Revoke a webhook",
    description="Stops deliveries; pending deliveries are abandoned. History stays readable.",
    responses={404: {"model": ErrorResponse}},
)
def revoke_webhook(
    webhook_id: uuid.UUID, application: CurrentApplication, db: DbSession
) -> WebhookResource:
    subscription = service.revoke_subscription(db, application, webhook_id)
    db.commit()
    return _resource(subscription)


@router.post(
    "/{webhook_id}/rotate",
    response_model=WebhookCreated,
    summary="Rotate the signing secret",
    description=(
        "Issues a new secret and keeps signing with the previous one as well for 24 hours, "
        "so the consumer can switch without rejecting deliveries."
    ),
    responses={404: {"model": ErrorResponse}},
)
def rotate_webhook(
    webhook_id: uuid.UUID, application: CurrentApplication, db: DbSession
) -> WebhookCreated:
    subscription, secret = service.rotate_secret(db, application, webhook_id)
    db.commit()
    return WebhookCreated(**_resource(subscription).model_dump(), secret=secret)


@router.get(
    "/{webhook_id}/deliveries",
    response_model=list[DeliveryResource],
    summary="Delivery history",
    responses={404: {"model": ErrorResponse}},
)
def list_deliveries(
    webhook_id: uuid.UUID,
    application: CurrentApplication,
    db: DbSession,
    state: Annotated[Literal["pending", "delivered", "abandoned"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeliveryResource]:
    rows = service.list_deliveries(
        db, application, webhook_id, state=state, limit=limit, offset=offset
    )
    return [_delivery(d) for d in rows]


@router.post(
    "/{webhook_id}/deliveries/{delivery_id}/replay",
    response_model=DeliveryResource,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay one delivery",
    description="Queues the same payload again. Consumers must deduplicate by event `id`.",
    responses={404: {"model": ErrorResponse}},
)
def replay_delivery(
    webhook_id: uuid.UUID, delivery_id: uuid.UUID, application: CurrentApplication, db: DbSession
) -> DeliveryResource:
    delivery = service.replay_delivery(db, application, webhook_id, delivery_id)
    db.commit()
    return _delivery(delivery)


@router.post(
    "/{webhook_id}/replay",
    response_model=ReplayResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay deliveries since a time",
    description=(
        "Re-queues every delivery created at or after `since`, delivered or not: the recovery "
        "path after an outage on the consumer side. Consumers must deduplicate by event `id` "
        "and order by `created_at`."
    ),
    responses={404: {"model": ErrorResponse}},
)
def replay_since(
    webhook_id: uuid.UUID,
    application: CurrentApplication,
    db: DbSession,
    since: Annotated[datetime, Query(description="ISO 8601 timestamp")],
) -> ReplayResult:
    count = service.replay_since(db, application, webhook_id, since)
    db.commit()
    return ReplayResult(requeued=count)
