"""Applications, their API credentials and their origins.

An application is the tenant boundary. Everything a credential can reach is
scoped to the application that issued it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.hostname import InvalidHostname, canonicalize
from app.models import (
    ApiCredential,
    Application,
    ApplicationStatus,
    OriginStatus,
    VerifiedOrigin,
)
from app.models.types import utcnow
from app.services.errors import (
    ApplicationAlreadyExists,
    ApplicationNotEmpty,
    ApplicationNotFound,
    ConfirmationMismatch,
    CredentialInUse,
    CredentialNotFound,
    InvalidApplication,
    InvalidCredential,
    InvalidOrigin,
    OriginConflict,
    OriginInUse,
    OriginNotVerified,
)

SECRET_PREFIX = "cd_"
SECRET_BYTES = 32
# Length of the non-secret identifier kept for logs: "cd_" plus eight characters.
KEY_PREFIX_LENGTH = 11
DEFAULT_PORTS = {"https": 443, "http": 80}

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


# --- applications -----------------------------------------------------------


def create_application(session: Session, *, slug: str, name: str, cname_target: str) -> Application:
    if not _SLUG_RE.match(slug or ""):
        raise InvalidApplication(
            "Slug must be lowercase letters, digits and hyphens (1-64 characters)"
        )
    if not name or not name.strip():
        raise InvalidApplication("Name is required")
    try:
        target = canonicalize(cname_target, allow_apex=True)
    except InvalidHostname as exc:
        raise InvalidApplication(f"CNAME target is not valid: {exc.message}") from exc

    application = Application(slug=slug, name=name.strip(), cname_target=target)
    try:
        with session.begin_nested():
            session.add(application)
            session.flush()
    except IntegrityError as exc:
        raise ApplicationAlreadyExists(f"Application '{slug}' already exists") from exc
    return application


def rename_application(session: Session, application: Application, name: str) -> Application:
    """Change the display name. The slug is the stable identifier and never changes."""
    if not name or not name.strip():
        raise InvalidApplication("Name is required")
    if len(name.strip()) > 200:
        raise InvalidApplication("Name must be at most 200 characters")
    application.name = name.strip()
    application.updated_at = utcnow()
    session.flush()
    return application


def live_domain_count(session: Session, application: Application) -> int:
    from app.models import Domain

    return (
        session.scalar(
            select(func.count())
            .select_from(Domain)
            .where(Domain.application_id == application.id, Domain.deleted_at.is_(None))
        )
        or 0
    )


def delete_application(
    session: Session,
    application: Application,
    *,
    confirm_slug: str,
    delete_domains: bool = False,
    now: datetime | None = None,
) -> int:
    """Delete the application; returns the number of live domains deleted with it.

    Nothing is removed yet, so the audit trail keeps its retention promise:
    every live domain becomes a tombstone with its claim revoked and the usual
    ``domain.deleted`` event, which the application's webhooks still receive
    (the worker revokes them once those deliveries are settled). API keys are
    revoked, origins retired, and the application is suspended and hidden.
    Its slug is freed at once. ``purge_tombstones`` removes the row and everything it
    owns once the retention period of its last domain has passed.

    ``confirm_slug`` must repeat the slug. While live domains exist the call
    is refused unless ``delete_domains`` is set, so customers' hostnames are
    never dropped by accident.
    """
    from app.models import Domain
    from app.services.domains import TOMBSTONE_RETENTION, delete_domain

    if (confirm_slug or "").strip() != application.slug:
        raise ConfirmationMismatch(
            f"Type the application's slug ({application.slug}) to confirm the deletion"
        )
    live = live_domain_count(session, application)
    if live and not delete_domains:
        raise ApplicationNotEmpty(
            f"Application '{application.slug}' still has {live} live domain(s); delete them "
            "first, or confirm that they are deleted with it",
            details={"live_domains": live},
        )
    now = now or utcnow()
    for domain_id in session.scalars(
        select(Domain.id).where(
            Domain.application_id == application.id, Domain.deleted_at.is_(None)
        )
    ).all():
        delete_domain(session, application, domain_id, now=now)
    for credential in list_credentials(session, application):
        if credential.revoked_at is None:
            credential.revoked_at = now
    for origin in list_origins(session, application):
        origin.is_active = False
        origin.status = OriginStatus.RETIRED
    # Webhooks stay active so the domain.deleted events queued above are
    # delivered; the webhook worker revokes them once nothing is pending
    # (app.webhooks.worker.settle_deleted_applications).
    latest_purge = session.scalar(
        select(func.max(Domain.purge_after)).where(Domain.application_id == application.id)
    )
    application.status = ApplicationStatus.SUSPENDED
    application.deleted_at = now
    application.purge_after = max(filter(None, [latest_purge, now + TOMBSTONE_RETENTION]))
    # Free the slug for a new application; the suffix cannot occur in a real
    # slug, so the archived row never collides with one.
    application.slug = f"{application.slug[:46]}~{application.id.hex[:12]}"
    application.updated_at = now
    session.flush()
    return live


def set_cname_target(session: Session, application: Application, cname_target: str) -> str:
    """Change the name new claims tell customers to CNAME to; returns the canonical name.

    Existing domains keep the target their live claim was issued with (that
    is what their customers published); re-issue a claim to move a domain
    to the new target.
    """
    try:
        target = canonicalize(cname_target, allow_apex=True)
    except InvalidHostname as exc:
        raise InvalidApplication(f"CNAME target is not valid: {exc.message}") from exc
    application.cname_target = target
    application.updated_at = utcnow()
    session.flush()
    return target


def edge_names(session: Session, application: Application) -> list[str]:
    """The application's CNAME target plus every target a live claim still names.

    After ``set_cname_target`` the old name stays in use until the last
    domain issued against it is re-issued or deleted, so it must keep
    resolving to the edge, keep its certificate and keep being checked.
    """
    from app.models import ClaimStatus, Domain, OwnershipClaim

    names = [application.cname_target]
    for target in session.scalars(
        select(OwnershipClaim.cname_target)
        .join(Domain, Domain.id == OwnershipClaim.domain_id)
        .where(
            Domain.application_id == application.id,
            Domain.deleted_at.is_(None),
            OwnershipClaim.status != ClaimStatus.REVOKED,
        )
        .distinct()
    ):
        if target not in names:
            names.append(target)
    return names


def get_application(session: Session, application_id: uuid.UUID) -> Application:
    application = session.get(Application, application_id)
    if application is None or application.is_deleted:
        raise ApplicationNotFound()
    return application


def get_application_by_slug(
    session: Session, slug: str, *, include_deleted: bool = False
) -> Application:
    """The application with ``slug``; deleted (archived) ones only when asked for.

    A deleted application keeps a suffixed slug (``acme~1a2b3c4d5e6f``) until
    it is purged, so its retained records can still be read by that slug.
    """
    query = select(Application).where(Application.slug == slug)
    if not include_deleted:
        query = query.where(Application.deleted_at.is_(None))
    application = session.scalar(query)
    if application is None:
        raise ApplicationNotFound(f"Application '{slug}' not found")
    return application


def list_applications(session: Session) -> list[Application]:
    return list(
        session.scalars(
            select(Application).where(Application.deleted_at.is_(None)).order_by(Application.slug)
        )
    )


def list_deleted_applications(session: Session) -> list[Application]:
    """Deleted applications whose records are still retained, newest first."""
    return list(
        session.scalars(
            select(Application)
            .where(Application.deleted_at.is_not(None))
            .order_by(Application.deleted_at.desc())
        )
    )


def set_application_status(
    session: Session, application: Application, status: ApplicationStatus
) -> Application:
    application.status = status
    session.flush()
    return application


# --- credentials ------------------------------------------------------------


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)


def hash_secret(secret: str) -> str:
    # Secrets are high-entropy random strings, so an unsalted SHA-256 is an
    # appropriate one-way lookup key (same trade-off as GitHub/Stripe tokens).
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_credential(
    session: Session,
    application: Application,
    *,
    label: str,
    expires_at: datetime | None = None,
) -> tuple[ApiCredential, str]:
    """Create a credential and return it with the plaintext secret (shown once)."""
    if not label or not label.strip():
        raise InvalidCredential("Label is required")
    secret = generate_secret()
    credential = ApiCredential(
        application_id=application.id,
        label=label.strip(),
        key_prefix=secret[:KEY_PREFIX_LENGTH],
        key_hash=hash_secret(secret),
        expires_at=expires_at,
    )
    session.add(credential)
    session.flush()
    return credential, secret


def authenticate_credential(
    session: Session, secret: str | None, *, now: datetime | None = None
) -> ApiCredential:
    """Resolve a plaintext secret to a usable credential or raise ``InvalidCredential``.

    The returned credential has ``application`` loaded; callers scope every
    later query to it.
    """
    if not secret or not secret.startswith(SECRET_PREFIX):
        raise InvalidCredential()
    now = now or utcnow()
    digest = hash_secret(secret)
    credential = session.scalar(
        select(ApiCredential)
        .where(ApiCredential.key_hash == digest)
        .options(joinedload(ApiCredential.application))
    )
    if credential is None or not hmac.compare_digest(credential.key_hash, digest):
        raise InvalidCredential()
    if not credential.is_usable(now):
        raise InvalidCredential()
    if credential.application.status != ApplicationStatus.ACTIVE:
        raise InvalidCredential()
    credential.last_used_at = now
    session.flush()
    return credential


def list_credentials(session: Session, application: Application) -> list[ApiCredential]:
    return list(
        session.scalars(
            select(ApiCredential)
            .where(ApiCredential.application_id == application.id)
            .order_by(ApiCredential.created_at)
        )
    )


def revoke_credential(
    session: Session,
    application: Application,
    credential_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ApiCredential:
    credential = session.scalar(
        select(ApiCredential).where(
            ApiCredential.id == credential_id,
            ApiCredential.application_id == application.id,
        )
    )
    if credential is None:
        raise CredentialNotFound()
    if credential.revoked_at is None:
        credential.revoked_at = now or utcnow()
        session.flush()
    return credential


def delete_credential(
    session: Session,
    application: Application,
    credential_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> None:
    """Remove a credential that can no longer authenticate (revoked or expired)."""
    credential = session.scalar(
        select(ApiCredential).where(
            ApiCredential.id == credential_id,
            ApiCredential.application_id == application.id,
        )
    )
    if credential is None:
        raise CredentialNotFound()
    if credential.is_usable(now or utcnow()):
        raise CredentialInUse("Revoke the credential before deleting it")
    session.delete(credential)
    session.flush()


def rotate_credential(
    session: Session,
    application: Application,
    credential_id: uuid.UUID,
    *,
    label: str | None = None,
    grace: timedelta = timedelta(hours=24),
    now: datetime | None = None,
) -> tuple[ApiCredential, str, ApiCredential]:
    """Issue a replacement credential and let the old one expire after ``grace``.

    Returns ``(new_credential, plaintext_secret, old_credential)``. The old
    credential keeps working during the grace period so clients can switch
    without downtime; it is not extended if it already expires sooner.
    """
    now = now or utcnow()
    old = session.scalar(
        select(ApiCredential).where(
            ApiCredential.id == credential_id,
            ApiCredential.application_id == application.id,
        )
    )
    if old is None:
        raise CredentialNotFound()
    new, secret = issue_credential(session, application, label=label or old.label)
    cutoff = now + grace
    if old.revoked_at is None and (old.expires_at is None or old.expires_at > cutoff):
        old.expires_at = cutoff
    session.flush()
    return new, secret, old


def get_origin(
    session: Session,
    application: Application,
    *,
    origin_id: uuid.UUID | None = None,
    host: str | None = None,
) -> VerifiedOrigin:
    query = select(VerifiedOrigin).where(VerifiedOrigin.application_id == application.id)
    if origin_id is not None:
        query = query.where(VerifiedOrigin.id == origin_id)
    elif host is not None:
        query = query.where(VerifiedOrigin.host == canonicalize(host, allow_apex=True))
    else:
        raise InvalidOrigin("Give an origin id or host")
    origin = session.scalar(query.order_by(VerifiedOrigin.created_at.desc()))
    if origin is None:
        raise InvalidOrigin("No such origin for this application")
    return origin


def list_origins(session: Session, application: Application) -> list[VerifiedOrigin]:
    return list(
        session.scalars(
            select(VerifiedOrigin)
            .where(VerifiedOrigin.application_id == application.id)
            .order_by(VerifiedOrigin.created_at)
        )
    )


# --- origins ----------------------------------------------------------------


def register_origin(
    session: Session,
    application: Application,
    *,
    host: str,
    scheme: str = "https",
    port: int | None = None,
) -> VerifiedOrigin:
    scheme = (scheme or "").lower()
    if scheme not in DEFAULT_PORTS:
        raise InvalidOrigin("Scheme must be https or http")
    try:
        host = canonicalize(host, allow_apex=True)
    except InvalidHostname as exc:
        raise InvalidOrigin(f"Origin host is not valid: {exc.message}") from exc
    port = port or DEFAULT_PORTS[scheme]
    if not 0 < port < 65536:
        raise InvalidOrigin("Port must be between 1 and 65535")

    origin = VerifiedOrigin(
        application_id=application.id,
        scheme=scheme,
        host=host,
        port=port,
        status=OriginStatus.PENDING,
        verification_token=secrets.token_urlsafe(24),
    )
    try:
        with session.begin_nested():
            session.add(origin)
            session.flush()
    except IntegrityError as exc:
        raise OriginConflict(f"Origin {scheme}://{host}:{port} is already registered") from exc
    return origin


def record_origin_verification(
    session: Session,
    origin: VerifiedOrigin,
    *,
    verified: bool,
    error_code: str | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> VerifiedOrigin:
    now = now or utcnow()
    origin.last_checked_at = now
    if verified:
        origin.status = OriginStatus.VERIFIED
        origin.verified_at = now
        origin.last_error_code = None
        origin.last_error_message = None
    else:
        origin.status = OriginStatus.FAILED
        origin.last_error_code = error_code or "origin_verification_failed"
        origin.last_error_message = message
        # An origin that failed verification must not keep receiving customer
        # traffic; activation requires a fresh successful verification.
        origin.is_active = False
    session.flush()
    return origin


def activate_origin(session: Session, origin: VerifiedOrigin) -> VerifiedOrigin:
    """Make ``origin`` the single active origin of its application."""
    if origin.status != OriginStatus.VERIFIED:
        raise OriginNotVerified("Only a verified origin can be activated")
    others = session.scalars(
        select(VerifiedOrigin).where(
            VerifiedOrigin.application_id == origin.application_id,
            VerifiedOrigin.id != origin.id,
            VerifiedOrigin.is_active.is_(True),
        )
    ).all()
    for other in others:
        other.is_active = False
    # Two flushes so the partial unique index never sees two active rows.
    session.flush()
    origin.is_active = True
    session.flush()
    return origin


def delete_origin(session: Session, origin: VerifiedOrigin) -> None:
    """Remove an origin that does not carry traffic (pending, failed or retired)."""
    if origin.is_active:
        raise OriginInUse("The active origin carries the application's traffic; retire it first")
    session.delete(origin)
    session.flush()


def retire_origin(session: Session, origin: VerifiedOrigin) -> VerifiedOrigin:
    origin.is_active = False
    origin.status = OriginStatus.RETIRED
    session.flush()
    return origin


def get_active_origin(session: Session, application: Application) -> VerifiedOrigin | None:
    return session.scalar(
        select(VerifiedOrigin).where(
            VerifiedOrigin.application_id == application.id,
            VerifiedOrigin.is_active.is_(True),
        )
    )
