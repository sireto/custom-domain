"""The operator API: applications, origins and credentials over HTTP.

It is the ``custom-domain`` command for programs, such as a control plane
that provisions applications on a shared deployment. Every endpoint calls
the same service function as the command and the portal. It covers only
what the v1 API cannot do: v1 is scoped to one application by its
credential, so domains and webhooks stay there (issue the application a
credential here, then use v1 with it).

Enabled by ``OPERATOR_API_TOKEN`` (at least 32 characters); without it every
path answers 404. Callers send ``Authorization: Bearer <token>``. Private and
loopback peers may call it directly on the API's port; public addresses only
through the edge and only when listed in ``OPERATOR_ALLOWED_IPS`` (the edge
enforces the list on the real peer, and this module checks the address the
edge forwards again). Failed tokens are throttled per client like v1.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.clients import client_address
from app.db.session import get_session
from app.edge.settings import MIN_OPERATOR_TOKEN_LENGTH as MIN_TOKEN_LENGTH
from app.models import ApplicationStatus
from app.models.types import utcnow
from app.operator import schemas
from app.services import applications as app_service
from app.services import assertion_keys, operator_token, traffic
from app.services import domains as domain_service
from app.services.errors import RateLimited
from app.services.origin_verification import (
    OriginVerificationFailed,
    allow_private_from_env,
    verify_origin,
)
from app.v1.errors import ApiError
from app.v1.schemas import ErrorResponse
from app.v1.throttle import limited_client

logger = logging.getLogger(__name__)


bearer = HTTPBearer(
    auto_error=False,
    scheme_name="OperatorToken",
    description="The deployment's OPERATOR_API_TOKEN, as `Authorization: Bearer <token>`.",
)

router = APIRouter(
    prefix="/operator/v1",
    tags=["Operator"],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or wrong operator token."},
        403: {"model": ErrorResponse, "description": "The client address may not use it."},
        404: {"model": ErrorResponse, "description": "Not found, or the operator API is off."},
        422: {"model": ErrorResponse, "description": "The request is not valid."},
        429: {"model": ErrorResponse, "description": "Too many failed tokens from this client."},
    },
)

DbSession = Annotated[Session, Depends(get_session)]


def operator_token_from_env(env) -> str | None:
    token = env.get("OPERATOR_API_TOKEN", "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        logger.warning(
            "OPERATOR_API_TOKEN is shorter than %d characters; the operator API stays off",
            MIN_TOKEN_LENGTH,
        )
        return None
    return token


def require_operator(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: DbSession,
) -> None:
    token = getattr(request.app.state, "operator_token", None)
    if token is None:
        raise ApiError(404, "not_found", "Not found")
    settings = getattr(request.app.state, "edge_settings", None)
    address = client_address(request)
    if settings is None or not settings.operator_allows(address):
        raise ApiError(
            403,
            "address_not_allowed",
            "This address may not use the operator API (OPERATOR_ALLOWED_IPS)",
        )
    limiter = getattr(request.app.state, "operator_auth_limiter", None)
    client = limited_client(request) if limiter is not None and limiter.enabled else None
    if client is not None:
        wait = limiter.retry_after(client)
        if wait:
            raise RateLimited(
                "Too many requests with a wrong operator token from this address",
                retry_after=wait,
            )
    given = credentials.credentials if credentials else ""
    # A token set through PUT /operator/v1/token replaces the env one.
    if not (
        credentials
        and credentials.scheme.lower() == "bearer"
        and operator_token.matches(db, given, token)
    ):
        if client is not None:
            limiter.record_failure(client)
        raise ApiError(
            401,
            "unauthorized",
            "A valid operator token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


Operator = Depends(require_operator)


def _application(db: Session, slug: str):
    return app_service.get_application_by_slug(db, slug)


def _resource(db: Session, application) -> schemas.ApplicationResource:
    return schemas.ApplicationResource.of(
        application, app_service.live_domain_count(db, application)
    )


# --- applications ------------------------------------------------------------------


@router.get("/applications", dependencies=[Operator])
def list_applications(db: DbSession) -> list[schemas.ApplicationResource]:
    return [_resource(db, a) for a in app_service.list_applications(db)]


@router.post("/applications", status_code=status.HTTP_201_CREATED, dependencies=[Operator])
def create_application(
    request: Request, body: schemas.ApplicationCreate, db: DbSession
) -> schemas.ApplicationResource:
    target = body.cname_target
    if not target:
        settings = getattr(request.app.state, "edge_settings", None)
        target = settings.edge_hostname if settings else None
        if not target:
            raise ApiError(
                422,
                "invalid_application",
                "cname_target is required when EDGE_HOSTNAME is not set",
                details={"field": "cname_target"},
            )
    request.state.operator_target = f"application:{body.slug}"
    application = app_service.create_application(
        db, slug=body.slug, name=body.name, cname_target=target
    )
    db.commit()
    return _resource(db, application)


@router.get("/applications/{slug}", dependencies=[Operator])
def get_application(slug: str, db: DbSession) -> schemas.ApplicationResource:
    return _resource(db, _application(db, slug))


@router.patch("/applications/{slug}", dependencies=[Operator])
def update_application(
    slug: str, body: schemas.ApplicationUpdate, db: DbSession
) -> schemas.ApplicationResource:
    application = _application(db, slug)
    if body.name is not None:
        app_service.rename_application(db, application, body.name)
    if body.cname_target is not None:
        app_service.set_cname_target(db, application, body.cname_target)
        if body.reissue_claims:
            domain_service.reissue_claims_for_target(db, application)
    if body.workspace_probe is not None:
        application.workspace_probe_enabled = body.workspace_probe
    if body.status is not None:
        app_service.set_application_status(db, application, ApplicationStatus(body.status))
    rate_fields = {"rate_limit_per_minute", "rate_limit_per_second"} & body.model_fields_set
    if rate_fields:
        app_service.set_rate_limits(
            db,
            application,
            per_minute=body.rate_limit_per_minute
            if "rate_limit_per_minute" in rate_fields
            else application.rate_limit_per_minute,
            per_second=body.rate_limit_per_second
            if "rate_limit_per_second" in rate_fields
            else application.rate_limit_per_second,
        )
    if "max_domains" in body.model_fields_set:
        app_service.set_domain_limit(db, application, body.max_domains)
    db.commit()
    return _resource(db, application)


@router.get("/applications/{slug}/traffic", dependencies=[Operator])
def application_traffic(
    slug: str,
    db: DbSession,
    days: Annotated[
        int, Query(ge=1, le=traffic.TRAFFIC_RETENTION_DAYS, description="UTC days, today included.")
    ] = 30,
) -> schemas.ApplicationTrafficResource:
    """The application's proxied requests and response bytes per day, counted at the edge."""
    application = _application(db, slug)
    rows = traffic.application_traffic(db, application, days=days)
    return schemas.ApplicationTrafficResource(
        application=application.slug,
        days=[
            schemas.TrafficDayResource(
                date=row.day, requests=row.requests, response_bytes=row.response_bytes
            )
            for row in rows
        ],
        requests=sum(row.requests for row in rows),
        response_bytes=sum(row.response_bytes for row in rows),
    )


@router.delete("/applications/{slug}", dependencies=[Operator])
def delete_application(
    slug: str,
    db: DbSession,
    confirm: Annotated[str, Query(description="Repeat the slug to confirm.")] = "",
    delete_domains: Annotated[
        bool, Query(description="Also delete its live domains (refused without it).")
    ] = False,
) -> schemas.ApplicationDeleted:
    application = _application(db, slug)
    removed = app_service.delete_application(
        db, application, confirm_slug=confirm, delete_domains=delete_domains
    )
    db.commit()
    return schemas.ApplicationDeleted(deleted=slug, live_domains_deleted=removed)


# --- origins -----------------------------------------------------------------------


def _origin(db: Session, slug: str, origin_id: uuid.UUID):
    return app_service.get_origin(db, _application(db, slug), origin_id=origin_id)


@router.get("/applications/{slug}/origins", dependencies=[Operator])
def list_origins(slug: str, db: DbSession) -> list[schemas.OriginResource]:
    return [
        schemas.OriginResource.of(o) for o in app_service.list_origins(db, _application(db, slug))
    ]


@router.post(
    "/applications/{slug}/origins", status_code=status.HTTP_201_CREATED, dependencies=[Operator]
)
def register_origin(
    request: Request, slug: str, body: schemas.OriginCreate, db: DbSession
) -> schemas.OriginResource:
    origin = app_service.register_origin(
        db,
        _application(db, slug),
        host=body.host,
        scheme=body.scheme,
        port=body.port,
        host_header=body.host_header,
    )
    db.commit()
    request.state.operator_target = f"origin:{origin.id}"
    return schemas.OriginResource.of(origin)


@router.patch("/applications/{slug}/origins/{origin_id}", dependencies=[Operator])
def update_origin(
    slug: str, origin_id: uuid.UUID, body: schemas.OriginUpdate, db: DbSession
) -> schemas.OriginResource:
    """Change the Host the edge sends this origin; the next reconcile applies it."""
    origin = app_service.set_origin_host_header(db, _origin(db, slug, origin_id), body.host_header)
    db.commit()
    return schemas.OriginResource.of(origin)


@router.post("/applications/{slug}/origins/{origin_id}/verify", dependencies=[Operator])
def verify_origin_endpoint(
    slug: str, origin_id: uuid.UUID, db: DbSession, body: schemas.OriginVerify | None = None
) -> schemas.OriginResource:
    origin = _origin(db, slug, origin_id)
    try:
        verify_origin(db, origin, allow_private=allow_private_from_env())
    except OriginVerificationFailed as exc:
        db.commit()  # the failure is recorded on the origin
        raise ApiError(
            422,
            "origin_verification_failed",
            exc.message,
            details={
                "reason": exc.code,
                "origin": schemas.OriginResource.of(origin).model_dump(mode="json"),
            },
        ) from exc
    if body is None or body.activate:
        app_service.activate_origin(db, origin)
    db.commit()
    return schemas.OriginResource.of(origin)


@router.post("/applications/{slug}/origins/{origin_id}/activate", dependencies=[Operator])
def activate_origin(slug: str, origin_id: uuid.UUID, db: DbSession) -> schemas.OriginResource:
    origin = app_service.activate_origin(db, _origin(db, slug, origin_id))
    db.commit()
    return schemas.OriginResource.of(origin)


@router.post("/applications/{slug}/origins/{origin_id}/retire", dependencies=[Operator])
def retire_origin(slug: str, origin_id: uuid.UUID, db: DbSession) -> schemas.OriginResource:
    origin = app_service.retire_origin(db, _origin(db, slug, origin_id))
    db.commit()
    return schemas.OriginResource.of(origin)


@router.delete(
    "/applications/{slug}/origins/{origin_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Operator],
)
def delete_origin(slug: str, origin_id: uuid.UUID, db: DbSession) -> Response:
    app_service.delete_origin(db, _origin(db, slug, origin_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- credentials --------------------------------------------------------------------


def _assertion_key(key, signing, now) -> schemas.AssertionKeyResource:
    return schemas.AssertionKeyResource(
        key_id=key.key_id,
        state=assertion_keys.key_state(key, signing, now),
        active_from=key.active_from,
        created_at=key.created_at,
        revoked_at=key.revoked_at,
    )


@router.get("/applications/{slug}/assertion-keys", dependencies=[Operator])
def list_assertion_keys(slug: str, db: DbSession) -> schemas.AssertionKeys:
    """The application's own assertion keys (no secrets), and which one signs now."""
    application = _application(db, slug)
    now = utcnow()
    signing = assertion_keys.signing_key(db, application.id, now=now)
    return schemas.AssertionKeys(
        application_id=application.id,
        signing=signing.key_id if signing else None,
        keys=[_assertion_key(k, signing, now) for k in assertion_keys.list_keys(db, application)],
    )


@router.post(
    "/applications/{slug}/assertion-keys",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Operator],
)
def issue_assertion_key(
    request: Request, slug: str, db: DbSession, body: schemas.AssertionKeyCreate | None = None
) -> schemas.NewAssertionKey:
    """Issue the application's next assertion key; the secret is in this response only."""
    body = body or schemas.AssertionKeyCreate()
    application = _application(db, slug)
    key, secret = assertion_keys.issue_key(
        db, application, activate_in=timedelta(hours=body.activate_in_hours)
    )
    db.commit()
    request.state.operator_target = f"assertion_key:{key.key_id}"  # the id, never the secret
    now = utcnow()
    signing = assertion_keys.signing_key(db, application.id, now=now)
    return schemas.NewAssertionKey(
        **_assertion_key(key, signing, now).model_dump(),
        application_id=application.id,
        secret=secret,
    )


@router.post("/applications/{slug}/assertion-keys/{key_id}/revoke", dependencies=[Operator])
def revoke_assertion_key(slug: str, key_id: str, db: DbSession) -> schemas.AssertionKeyResource:
    """Stop signing with a key now; the previous key, or the deployment key, takes over."""
    application = _application(db, slug)
    key = assertion_keys.revoke_key(db, application, key_id)
    db.commit()
    now = utcnow()
    return _assertion_key(key, assertion_keys.signing_key(db, application.id, now=now), now)


@router.get("/applications/{slug}/credentials", dependencies=[Operator])
def list_credentials(slug: str, db: DbSession) -> list[schemas.CredentialResource]:
    return [
        schemas.CredentialResource.of(c)
        for c in app_service.list_credentials(db, _application(db, slug))
    ]


@router.post(
    "/applications/{slug}/credentials", status_code=status.HTTP_201_CREATED, dependencies=[Operator]
)
def issue_credential(
    request: Request, slug: str, body: schemas.CredentialCreate, db: DbSession
) -> schemas.NewCredential:
    expires_at = utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    credential, secret = app_service.issue_credential(
        db, _application(db, slug), label=body.label, expires_at=expires_at
    )
    db.commit()
    request.state.operator_target = f"credential:{credential.id}"  # the id, never the secret
    return schemas.NewCredential(
        **schemas.CredentialResource.of(credential).model_dump(), secret=secret
    )


@router.post(
    "/applications/{slug}/credentials/{credential_id}/rotate",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Operator],
)
def rotate_credential(
    request: Request,
    slug: str,
    credential_id: uuid.UUID,
    db: DbSession,
    body: schemas.CredentialRotate | None = None,
) -> schemas.NewCredential:
    grace = timedelta(hours=(body.grace_hours if body else 24))
    credential, secret, _old = app_service.rotate_credential(
        db, _application(db, slug), credential_id, grace=grace
    )
    db.commit()
    request.state.operator_target = (
        f"credential:{credential.id}"  # the new one; the path names the old
    )
    return schemas.NewCredential(
        **schemas.CredentialResource.of(credential).model_dump(), secret=secret
    )


@router.post("/applications/{slug}/credentials/{credential_id}/revoke", dependencies=[Operator])
def revoke_credential(
    slug: str, credential_id: uuid.UUID, db: DbSession
) -> schemas.CredentialResource:
    credential = app_service.revoke_credential(db, _application(db, slug), credential_id)
    db.commit()
    return schemas.CredentialResource.of(credential)


@router.delete(
    "/applications/{slug}/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Operator],
)
def delete_credential(slug: str, credential_id: uuid.UUID, db: DbSession) -> Response:
    app_service.delete_credential(db, _application(db, slug), credential_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- health --------------------------------------------------------------------------


@router.put(
    "/token",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Operator],
    summary="Replace the operator token",
    description=(
        "Sets a new operator token, effective at once on every API instance. Only its "
        "SHA-256 is stored, and the previous token (including `OPERATOR_API_TOKEN`) stops "
        "working. Use it to retire a token that was handed out at install time. "
        "`custom-domain operator reset-token` on the host makes `OPERATOR_API_TOKEN` "
        "valid again."
    ),
)
def set_operator_token(request: Request, body: schemas.OperatorTokenSet, db: DbSession) -> Response:
    request.state.operator_target = "operator-token"
    operator_token.set_token(db, body.token)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/backup",
    dependencies=[Operator],
    summary="Download a database backup",
    description=(
        "Streams `pg_dump --format=custom` of the deployment's database; restore it with "
        "`pg_restore` (docs/operations.md). It contains claim tokens, credential hashes and "
        "webhook signing secrets: store it like a secrets file. Off (404) unless "
        "`OPERATOR_BACKUP=true`. PostgreSQL only; `409 backup_unavailable` otherwise. "
        "One at a time: `429` while another runs."
    ),
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def backup(request: Request, db: DbSession):
    from fastapi.responses import StreamingResponse

    from app.db.session import get_database_url
    from app.services import backup as backup_service

    if not backup_service.enabled():
        # Off by default: the dump holds every webhook signing secret.
        raise ApiError(404, "not_found", "Not found")
    request.state.operator_target = "backup"
    # The download can take minutes: give the request's database connection
    # back to the pool now, not when the response has been sent.
    db.close()
    chunks = backup_service.stream(get_database_url())
    name = f"custom-domain-{utcnow().strftime('%Y-%m-%dT%H%M%SZ')}.dump"
    return StreamingResponse(
        chunks,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/doctor", dependencies=[Operator])
def doctor(request: Request) -> schemas.DoctorReport:
    """The same checks as `custom-domain doctor`, for monitoring a deployment."""
    from app.db.session import get_session_factory
    from app.dns.settings import DnsSettings
    from app.services.doctor import run_doctor, summarize

    findings = run_doctor(
        get_session_factory(), request.app.state.edge_settings, DnsSettings.from_env()
    )
    ok, warn, fail = summarize(findings)
    return schemas.DoctorReport(
        ok=ok,
        warn=warn,
        fail=fail,
        findings=[
            schemas.Finding(check=f.check, status=f.status, detail=f.detail) for f in findings
        ],
    )
