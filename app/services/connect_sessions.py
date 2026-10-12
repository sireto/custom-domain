"""Connect sessions: a link an application gives its customer to set up one domain.

``POST /v1/domains/{id}/connect-session`` returns
``https://<EDGE_HOSTNAME>/connect/<token>``. The page there (app/connect/)
shows the domain's DNS records, its provider, the live check states, the
provider's guide and, when the provider supports it, the Domain Connect
button. The link is the only credential the customer's browser holds; it
never sees the application's API key.

* **The token** is 32 random bytes (43 URL-safe characters). Only its SHA-256
  is stored, so a database dump does not reveal a usable link; the token
  appears once, in the response that created the session, and is never
  logged (the log redaction masks ``/connect/<token>``).
* **Its powers:** read one domain's setup and status, ask for a check (the
  same limits as ``POST /v1/domains/{id}/checks``) and start a Domain Connect
  apply (the same limits as ``POST /v1/domains/{id}/connect``). Nothing else.
* **Its end:** ``expires_at`` (24 hours by default, at least 5 minutes) or
  revocation. A wrong, expired or revoked token, a deleted domain and an
  application that is no longer active all look the same: not found.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationStatus, ConnectSession, Domain
from app.models.types import utcnow
from app.services.errors import (
    ConnectPageUnavailable,
    ConnectSessionNotFound,
    InvalidConnectRequest,
    RateLimited,
)

audit = logging.getLogger("app.connect.audit")

PAGE_PATH = "/connect"
DEFAULT_TTL = timedelta(hours=24)
MIN_TTL = timedelta(minutes=5)
MAX_TTL = timedelta(hours=24)
# Sessions created, counted from the table: per domain and per application.
WINDOW = timedelta(hours=1)
DOMAIN_MAX_PER_WINDOW = 20
APPLICATION_MAX_PER_WINDOW = 600
MAX_APP_NAME = 64
TOKEN_BYTES = 32
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def page_base_url(edge_settings) -> str | None:
    """``https://<EDGE_HOSTNAME>`` when the edge serves this service's public paths."""
    if edge_settings is not None and edge_settings.public_api and edge_settings.edge_hostname:
        return f"https://{edge_settings.edge_hostname}"
    return None


def page_url(base_url: str, token: str) -> str:
    return f"{base_url}{PAGE_PATH}/{token}"


def app_name_problem(value: str) -> str | None:
    if not value.strip():
        return "must not be blank"
    if len(value) > MAX_APP_NAME:
        return f"must be at most {MAX_APP_NAME} characters"
    if any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in value):
        return "must not contain control characters"
    return None


@dataclass(frozen=True)
class Created:
    session: ConnectSession
    url: str


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _enforce_limits(session: Session, domain: Domain, now: datetime) -> None:
    for scope, condition, limit in (
        ("domain", ConnectSession.domain_id == domain.id, DOMAIN_MAX_PER_WINDOW),
        (
            "application",
            ConnectSession.application_id == domain.application_id,
            APPLICATION_MAX_PER_WINDOW,
        ),
    ):
        filters = [condition, ConnectSession.created_at > now - WINDOW]
        count = session.scalar(select(func.count()).select_from(ConnectSession).where(*filters))
        if count is not None and count >= limit:
            oldest = _as_utc(
                session.scalar(select(func.min(ConnectSession.created_at)).where(*filters))
            )
            wait = (oldest + WINDOW - now).total_seconds() if oldest else 60
            raise RateLimited(
                f"At most {limit} connect sessions per {scope} per hour",
                retry_after=math.ceil(wait),
            )


def create_session(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    base_url: str | None,
    ttl_seconds: int | None = None,
    app_name: str | None = None,
    return_url: str | None = None,
    now: datetime | None = None,
) -> Created:
    """A new session for one live domain of ``application``. The caller commits.

    The returned URL holds the token; it is not stored and must not be logged.
    """
    from app.services.domain_connect import redirect_uri_problem
    from app.services.domains import get_domain, lock_application

    now = now or utcnow()
    ttl = DEFAULT_TTL if ttl_seconds is None else timedelta(seconds=ttl_seconds)
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise InvalidConnectRequest(
            f"ttl_seconds must be between {int(MIN_TTL.total_seconds())} and "
            f"{int(MAX_TTL.total_seconds())}",
            details={"field": "ttl_seconds"},
        )
    if app_name is not None:
        if problem := app_name_problem(app_name):
            raise InvalidConnectRequest(f"app_name {problem}", details={"field": "app_name"})
        app_name = app_name.strip()
    if return_url is not None and (problem := redirect_uri_problem(return_url)):
        raise InvalidConnectRequest(f"return_url {problem}", details={"field": "return_url"})
    # The first statement of this transaction (see lock_application), so two
    # requests cannot both pass the limits.
    lock_application(session, application.id)
    domain = get_domain(session, application, domain_id)
    if base_url is None:
        raise ConnectPageUnavailable(
            "This deployment serves no setup page: it needs PUBLIC_API=true and EDGE_HOSTNAME"
        )
    _enforce_limits(session, domain, now)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = ConnectSession(
        application_id=application.id,
        domain_id=domain.id,
        token_hash=hash_token(token),
        expires_at=now + ttl,
        app_name=app_name,
        return_url=return_url,
        created_at=now,
    )
    session.add(row)
    session.flush()
    audit.info(
        "connect session %s created: application %s domain %s expires %s",
        row.id,
        application.slug,
        domain.id,
        row.expires_at.isoformat(),
    )
    return Created(row, page_url(base_url, token))


def revoke_session(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ConnectSession:
    """End a session now. Revoking a revoked or expired session is a no-op."""
    from app.services.domains import get_domain

    domain = get_domain(session, application, domain_id, include_deleted=True)
    row = session.scalar(
        select(ConnectSession).where(
            ConnectSession.id == session_id,
            ConnectSession.domain_id == domain.id,
            ConnectSession.application_id == application.id,
        )
    )
    if row is None:
        raise ConnectSessionNotFound("No such connect session for this domain")
    if row.revoked_at is None:
        row.revoked_at = now or utcnow()
        session.flush()
        audit.info(
            "connect session %s revoked: application %s domain %s",
            row.id,
            application.slug,
            domain.id,
        )
    return row


@dataclass(frozen=True)
class Opened:
    session_id: uuid.UUID
    application_id: uuid.UUID
    domain_id: uuid.UUID
    expires_at: datetime
    app_name: str | None
    return_url: str | None


def open_session(session: Session, token: str, *, now: datetime | None = None) -> Opened | None:
    """The live session for ``token``, or None, the same for every reason it is not.

    Live means: not revoked, not expired, its domain not deleted and its
    application active.
    """
    if not TOKEN.match(token or ""):
        return None
    digest = hash_token(token)
    row = session.scalar(select(ConnectSession).where(ConnectSession.token_hash == digest))
    # The lookup is by hash; this comparison only makes the match explicit.
    if row is None or not hmac.compare_digest(row.token_hash, digest):
        return None
    now = now or utcnow()
    if row.revoked_at is not None or _as_utc(row.expires_at) <= now:
        return None
    live = session.scalar(
        select(Domain.id)
        .join(Application, Application.id == Domain.application_id)
        .where(
            Domain.id == row.domain_id,
            Domain.application_id == row.application_id,
            Domain.deleted_at.is_(None),
            Application.status == ApplicationStatus.ACTIVE,
            Application.deleted_at.is_(None),
        )
    )
    if live is None:
        return None
    return Opened(
        session_id=row.id,
        application_id=row.application_id,
        domain_id=row.domain_id,
        expires_at=_as_utc(row.expires_at),
        app_name=row.app_name,
        return_url=row.return_url,
    )
