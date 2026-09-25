"""Webhook subscriptions and the transactional outbox.

Deliveries are enqueued in the same transaction as the domain event that
causes them (``record_event`` calls ``enqueue_for_event``), so an event is
never recorded without its deliveries or vice versa. Each delivery snapshots
the domain resource at that moment.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    Domain,
    DomainEvent,
    DomainStatus,
    EventType,
    WebhookDelivery,
    WebhookSubscription,
)
from app.models.types import utcnow
from app.services.errors import ServiceError

WEBHOOK_EVENT_TYPES = (
    "domain.ready",
    "domain.attention_required",
    "domain.recovered",
    "domain.deleted",
)
SECRET_PREFIX = "whsec_"
ROTATION_GRACE = timedelta(hours=24)
MAX_SUBSCRIPTIONS = 10


class WebhookNotFound(ServiceError):
    code = "webhook_not_found"


class InvalidWebhook(ServiceError):
    code = "invalid_webhook"


class DeliveryNotFound(ServiceError):
    code = "delivery_not_found"


def webhook_event_type(event: DomainEvent) -> str | None:
    """Map a recorded domain event to a webhook event type, or None if it has none."""
    payload = event.payload or {}
    if event.event_type == EventType.STATUS_CHANGED.value:
        target = payload.get("to")
        if target == DomainStatus.READY.value:
            return "domain.recovered" if payload.get("reason") == "recovered" else "domain.ready"
        if target == DomainStatus.ATTENTION_REQUIRED.value:
            return "domain.attention_required"
        return None
    if event.event_type == EventType.DOMAIN_DELETED.value:
        return "domain.deleted"
    return None


def validate_url(url: str, *, allow_private: bool = False) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise InvalidWebhook("Webhook URL must be an absolute http(s) URL")
    if parts.scheme == "http" and not allow_private:
        raise InvalidWebhook("Webhook URL must use https")
    if parts.username or parts.password:
        raise InvalidWebhook("Webhook URL must not contain credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    _require_public(parts.hostname, port, allow_private=allow_private)
    return url.strip()


def _require_public(host: str, port: int, *, allow_private: bool) -> None:
    """Refuse hosts that resolve to non-public addresses (same rule as origin verification)."""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InvalidWebhook(
            f"Webhook URL is not deliverable: {host} does not resolve ({exc})"
        ) from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise InvalidWebhook(f"Webhook URL is not deliverable: {host} has no addresses")
    if allow_private:
        return
    blocked = sorted(a for a in addresses if not ipaddress.ip_address(a).is_global)
    if blocked:
        raise InvalidWebhook(
            f"Webhook URL is not deliverable: {host} resolves to non-public address(es) "
            f"{', '.join(blocked)}; set ORIGIN_ALLOW_PRIVATE=true only for a trusted "
            "self-hosted deployment"
        )


def create_subscription(
    session: Session,
    application: Application,
    *,
    url: str,
    events: list[str],
    allow_private: bool = False,
) -> tuple[WebhookSubscription, str]:
    unknown = sorted(set(events) - set(WEBHOOK_EVENT_TYPES))
    if not events or unknown:
        raise InvalidWebhook(
            f"events must be a non-empty subset of {', '.join(WEBHOOK_EVENT_TYPES)}"
            + (f"; unknown: {', '.join(unknown)}" if unknown else "")
        )
    active = [s for s in list_subscriptions(session, application) if s.is_active]
    if len(active) >= MAX_SUBSCRIPTIONS:
        raise InvalidWebhook(f"At most {MAX_SUBSCRIPTIONS} active webhooks per application")
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    subscription = WebhookSubscription(
        application_id=application.id,
        url=validate_url(url, allow_private=allow_private),
        events=sorted(set(events)),
        secret=secret,
    )
    session.add(subscription)
    session.flush()
    return subscription, secret


def list_subscriptions(session: Session, application: Application) -> list[WebhookSubscription]:
    return list(
        session.scalars(
            select(WebhookSubscription)
            .where(WebhookSubscription.application_id == application.id)
            .order_by(WebhookSubscription.created_at)
        )
    )


def get_subscription(
    session: Session, application: Application, subscription_id: uuid.UUID
) -> WebhookSubscription:
    subscription = session.scalar(
        select(WebhookSubscription).where(
            WebhookSubscription.id == subscription_id,
            WebhookSubscription.application_id == application.id,
        )
    )
    if subscription is None:
        raise WebhookNotFound()
    return subscription


def revoke_subscription(
    session: Session, application: Application, subscription_id: uuid.UUID, *, now=None
) -> WebhookSubscription:
    subscription = get_subscription(session, application, subscription_id)
    if subscription.revoked_at is None:
        subscription.revoked_at = now or utcnow()
        session.flush()
    return subscription


def rotate_secret(
    session: Session,
    application: Application,
    subscription_id: uuid.UUID,
    *,
    grace: timedelta = ROTATION_GRACE,
    now: datetime | None = None,
) -> tuple[WebhookSubscription, str]:
    now = now or utcnow()
    subscription = get_subscription(session, application, subscription_id)
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    subscription.previous_secret = subscription.secret
    subscription.previous_secret_expires_at = now + grace
    subscription.secret = secret
    session.flush()
    return subscription, secret


def signing_secrets(subscription: WebhookSubscription, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    result = [subscription.secret]
    if (
        subscription.previous_secret
        and subscription.previous_secret_expires_at
        and subscription.previous_secret_expires_at > now
    ):
        result.append(subscription.previous_secret)
    return result


def build_payload(event: DomainEvent, webhook_type: str, domain: Domain) -> dict[str, Any]:
    from app.v1.schemas import domain_resource

    return {
        "id": str(event.id),
        "type": webhook_type,
        "created_at": event.created_at.isoformat(),
        "data": {"domain": domain_resource(domain).model_dump(mode="json")},
    }


def enqueue_for_event(
    session: Session, domain: Domain, event: DomainEvent
) -> list[WebhookDelivery]:
    """Create one pending delivery per active subscription that wants this event."""
    webhook_type = webhook_event_type(event)
    if webhook_type is None:
        return []
    subscriptions = session.scalars(
        select(WebhookSubscription).where(
            WebhookSubscription.application_id == domain.application_id,
            WebhookSubscription.revoked_at.is_(None),
        )
    ).all()
    wanted = [s for s in subscriptions if webhook_type in (s.events or [])]
    if not wanted:
        return []
    payload = build_payload(event, webhook_type, domain)
    deliveries = []
    for subscription in wanted:
        delivery = WebhookDelivery(
            subscription_id=subscription.id,
            application_id=domain.application_id,
            event_id=event.id,
            event_type=webhook_type,
            domain_id=domain.id,
            payload=payload,
            attempts=0,
            next_attempt_at=event.created_at,
        )
        session.add(delivery)
        deliveries.append(delivery)
    session.flush()
    return deliveries


def list_deliveries(
    session: Session,
    application: Application,
    subscription_id: uuid.UUID,
    *,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[WebhookDelivery]:
    subscription = get_subscription(session, application, subscription_id)
    query = select(WebhookDelivery).where(WebhookDelivery.subscription_id == subscription.id)
    if state == "delivered":
        query = query.where(WebhookDelivery.delivered_at.is_not(None))
    elif state == "abandoned":
        query = query.where(WebhookDelivery.abandoned_at.is_not(None))
    elif state == "pending":
        query = query.where(
            WebhookDelivery.delivered_at.is_(None), WebhookDelivery.abandoned_at.is_(None)
        )
    query = query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id)
    return list(session.scalars(query.limit(max(1, min(limit, 200))).offset(max(0, offset))))


def replay_delivery(
    session: Session,
    application: Application,
    subscription_id: uuid.UUID,
    delivery_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> WebhookDelivery:
    """Queue the same snapshot again; the consumer deduplicates by event id."""
    subscription = get_subscription(session, application, subscription_id)
    delivery = session.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.subscription_id == subscription.id,
        )
    )
    if delivery is None:
        raise DeliveryNotFound()
    delivery.delivered_at = None
    delivery.abandoned_at = None
    delivery.attempts = 0
    delivery.next_attempt_at = now or utcnow()
    session.flush()
    return delivery


def replay_since(
    session: Session,
    application: Application,
    subscription_id: uuid.UUID,
    since: datetime,
    *,
    now: datetime | None = None,
) -> int:
    """Re-queue every delivery of the subscription created at or after ``since``."""
    subscription = get_subscription(session, application, subscription_id)
    rows = session.scalars(
        select(WebhookDelivery).where(
            WebhookDelivery.subscription_id == subscription.id,
            WebhookDelivery.created_at >= since,
        )
    ).all()
    stamp = now or utcnow()
    for delivery in rows:
        delivery.delivered_at = None
        delivery.abandoned_at = None
        delivery.attempts = 0
        delivery.next_attempt_at = stamp
    session.flush()
    return len(rows)
