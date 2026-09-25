"""Delivers pending webhook deliveries with retries and backoff.

Deliveries are leased individually so several workers can run at once. A
delivery is attempted at most ``MAX_ATTEMPTS`` times on the ``BACKOFF``
schedule, then marked abandoned; abandoned and delivered deliveries stay in
the history and can be replayed through the API.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models import WebhookDelivery, WebhookSubscription
from app.models.types import utcnow
from app.services.webhooks import signing_secrets
from app.webhooks.signature import HEADER, sign

logger = logging.getLogger(__name__)

BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=12),
    timedelta(hours=24),
)
MAX_ATTEMPTS = 8
LEASE = timedelta(minutes=2)
TIMEOUT = 10.0
USER_AGENT = "custom-domain-webhooks/1"


@dataclass(frozen=True)
class DeliveryResult:
    at: datetime
    attempted: int
    delivered: int
    failed: int


def _due(now: datetime):
    return and_(
        WebhookDelivery.delivered_at.is_(None),
        WebhookDelivery.abandoned_at.is_(None),
        WebhookDelivery.next_attempt_at <= now,
    )


def due_delivery_ids(session: Session, now: datetime, limit: int) -> list[uuid.UUID]:
    rows = session.execute(
        select(WebhookDelivery.id)
        .where(_due(now))
        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
        .limit(limit)
    ).all()
    return [row[0] for row in rows]


def lease_delivery(session: Session, delivery_id: uuid.UUID, now: datetime) -> bool:
    result = session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id, _due(now))
        .values(next_attempt_at=now + LEASE)
    )
    return bool(result.rowcount)


Sender = Callable[[str, bytes, dict[str, str]], tuple[int, str]]


def http_sender(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        response = client.post(url, content=body, headers=headers)
    return response.status_code, response.text[:500]


def attempt_delivery(
    session_factory: Callable[[], Session],
    delivery_id: uuid.UUID,
    *,
    sender: Sender = http_sender,
    now: datetime | None = None,
) -> str:
    """Try one delivery. Returns 'skipped', 'delivered', 'retry' or 'abandoned'."""
    now = now or utcnow()
    with session_factory() as session:
        if not lease_delivery(session, delivery_id, now):
            session.rollback()
            return "skipped"
        session.commit()
        delivery = session.get(WebhookDelivery, delivery_id)
        subscription = session.get(WebhookSubscription, delivery.subscription_id)
        if subscription is None or not subscription.is_active:
            delivery.abandoned_at = now
            delivery.last_error = "subscription revoked"
            session.commit()
            return "abandoned"
        body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            HEADER: sign(body, signing_secrets(subscription, now), timestamp=int(now.timestamp())),
            "X-Custom-Domain-Event": delivery.event_type,
            "X-Custom-Domain-Event-Id": str(delivery.event_id),
            "X-Custom-Domain-Delivery-Id": str(delivery.id),
            "X-Custom-Domain-Attempt": str(delivery.attempts + 1),
        }
        url = subscription.url
        session.commit()  # no transaction while the request is in flight

        try:
            status, text = sender(url, body, headers)
            error = None if 200 <= status < 300 else f"HTTP {status}: {text[:200]}"
        except Exception as exc:  # network failures are retried like 5xx
            status, error = None, f"{type(exc).__name__}: {exc}"[:500]

        delivery = session.get(WebhookDelivery, delivery_id)
        delivery.attempts += 1
        delivery.last_attempt_at = now
        delivery.last_status = status
        delivery.last_error = error
        if error is None:
            delivery.delivered_at = now
            delivery.next_attempt_at = None
            outcome = "delivered"
        elif delivery.attempts >= MAX_ATTEMPTS:
            delivery.abandoned_at = now
            delivery.next_attempt_at = None
            outcome = "abandoned"
        else:
            delivery.next_attempt_at = now + BACKOFF[min(delivery.attempts, len(BACKOFF)) - 1]
            outcome = "retry"
        session.commit()
        return outcome


def deliver_due(
    session_factory: Callable[[], Session],
    *,
    sender: Sender = http_sender,
    now: datetime | None = None,
    limit: int = 100,
) -> DeliveryResult:
    now = now or utcnow()
    with session_factory() as session:
        ids = due_delivery_ids(session, now, limit)
    attempted = delivered = failed = 0
    for delivery_id in ids:
        try:
            outcome = attempt_delivery(session_factory, delivery_id, sender=sender, now=now)
        except Exception:
            failed += 1
            logger.exception("webhook delivery %s crashed", delivery_id)
            continue
        if outcome == "skipped":
            continue
        attempted += 1
        if outcome == "delivered":
            delivered += 1
        else:
            failed += 1
    return DeliveryResult(now, attempted, delivered, failed)


class WebhookWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        sender: Sender = http_sender,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender
        self.batch_size = batch_size
        self.last_result: DeliveryResult | None = None

    def run_once(self) -> DeliveryResult:
        try:
            result = deliver_due(self.session_factory, sender=self.sender, limit=self.batch_size)
        except Exception as exc:
            logger.error("webhook worker run failed: %s", exc)
            result = DeliveryResult(utcnow(), 0, 0, 0)
        self.last_result = result
        return result

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while True:
            self.run_once()
            if stop.wait(interval):
                return
