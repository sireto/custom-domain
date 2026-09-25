"""v1 domain endpoints. See docs/api-v1.md for the contract."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response, status

from app.models import DomainStatus
from app.services import domains as domain_service
from app.services import idempotency
from app.v1 import examples
from app.v1.deps import CurrentApplication, DbSession
from app.v1.schemas import (
    DomainCreate,
    DomainPage,
    DomainResource,
    ErrorResponse,
    domain_resource,
)

router = APIRouter(
    prefix="/v1",
    tags=["Domains"],
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Missing, invalid, revoked or expired credential.",
            "content": {"application/json": {"example": examples.ERRORS["unauthorized"]}},
        },
        403: {"model": ErrorResponse, "description": "The application is suspended."},
        422: {"model": ErrorResponse, "description": "The request is not valid."},
    },
)


@router.post(
    "/domains",
    status_code=status.HTTP_201_CREATED,
    response_model=DomainResource,
    summary="Register a hostname",
    description=(
        "Claims an exact customer subdomain for a workspace of the calling application "
        "and returns the DNS records the customer must publish. The hostname is "
        "normalized before it is stored. A hostname can be live in only one application; "
        "a second claim anywhere returns `hostname_already_claimed`.\n\n"
        "Send an `Idempotency-Key` header to make retries safe: a repeated key with the "
        "same body returns the original domain with status 200 and "
        "`Idempotent-Replayed: true`; a repeated key with a different body returns "
        "`idempotency_key_reused`. Keys are scoped to the application and expire after 24 hours."
    ),
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": examples.example(
                        "registration", examples.CREATE_REQUEST, "Register a workspace domain"
                    )
                }
            },
            "required": True,
        }
    },
    responses={
        201: {
            "description": "Domain registered; publish the returned DNS records.",
            "content": {
                "application/json": {
                    "examples": examples.example(
                        "registration", examples.REGISTRATION, "Registration: waiting for DNS"
                    )
                }
            },
        },
        200: {
            "model": DomainResource,
            "description": "Replay of an earlier request with the same Idempotency-Key.",
        },
        409: {
            "model": ErrorResponse,
            "description": "The hostname is already claimed.",
            "content": {
                "application/json": {"example": examples.ERRORS["hostname_already_claimed"]}
            },
        },
        422: {
            "model": ErrorResponse,
            "description": "Invalid hostname, reference, metadata or idempotency key reuse.",
            "content": {
                "application/json": {
                    "examples": {
                        "apex_not_supported": {"value": examples.ERRORS["apex_not_supported"]},
                        "idempotency_key_reused": {
                            "value": examples.ERRORS["idempotency_key_reused"]
                        },
                    }
                }
            },
        },
    },
)
def create_domain(
    payload: DomainCreate,
    response: Response,
    application: CurrentApplication,
    db: DbSession,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=idempotency.MAX_KEY_LENGTH,
            description="Client-chosen unique key that makes the create safe to retry.",
        ),
    ] = None,
) -> DomainResource:
    record = None
    if idempotency_key:
        request_hash = idempotency.fingerprint(payload.model_dump(mode="json"))
        record, created = idempotency.begin(db, application, idempotency_key, request_hash)
        if not created:
            if record.request_hash != request_hash:
                raise idempotency.IdempotencyKeyReused(
                    "Idempotency-Key was already used with a different request body"
                )
            if record.domain_id is None:
                raise idempotency.IdempotencyInProgress(
                    "A request with this Idempotency-Key is still being processed"
                )
            domain = domain_service.get_domain(
                db, application, record.domain_id, include_deleted=True
            )
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replayed"] = "true"
            return domain_resource(domain)

    from app import observability

    try:
        domain = domain_service.claim_domain(
            db, application, payload.hostname, payload.reference, metadata=payload.metadata
        )
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        observability.registrations_total.labels(outcome=code).inc()
        raise
    if record is not None:
        idempotency.complete(db, record, domain.id)
    db.commit()
    observability.registrations_total.labels(outcome="created").inc()
    return domain_resource(domain)


@router.get(
    "/domains",
    response_model=DomainPage,
    summary="List domains",
    description=(
        "Lists the calling application's domains, newest last, with offset pagination. "
        "Filter by workspace `reference` and by `status`. Deleted domains are excluded "
        "unless `include_deleted=true`."
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "examples": {
                        "mixed": {
                            "summary": "One ready domain and one waiting for DNS",
                            "value": {
                                "items": [examples.READY, examples.WAITING_FOR_DNS],
                                "limit": 50,
                                "offset": 0,
                                "next_offset": None,
                            },
                        }
                    }
                }
            }
        }
    },
)
def list_domains(
    application: CurrentApplication,
    db: DbSession,
    reference: Annotated[str | None, Query(max_length=255)] = None,
    domain_status: Annotated[DomainStatus | None, Query(alias="status")] = None,
    include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=domain_service.MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DomainPage:
    rows, has_more = domain_service.page_domains(
        db,
        application,
        reference=reference,
        status=domain_status,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    items = [domain_resource(d) for d in rows]
    return DomainPage(
        items=items, limit=limit, offset=offset, next_offset=offset + limit if has_more else None
    )


@router.get(
    "/domains/{domain_id}",
    response_model=DomainResource,
    summary="Get a domain",
    description=(
        "Returns one domain of the calling application with its current checks. A domain "
        "that belongs to another application is reported as not found."
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "examples": {
                        "waiting_for_dns": {
                            "summary": "Waiting for DNS: records not found yet",
                            "value": examples.WAITING_FOR_DNS,
                        },
                        "ready": {"summary": "Ready and serving", "value": examples.READY},
                        "dns_drift": {
                            "summary": "DNS drift: CNAME changed after readiness",
                            "value": examples.DNS_DRIFT,
                        },
                    }
                }
            }
        },
        404: {
            "model": ErrorResponse,
            "content": {"application/json": {"example": examples.ERRORS["domain_not_found"]}},
        },
    },
)
def get_domain(
    domain_id: uuid.UUID,
    application: CurrentApplication,
    db: DbSession,
    include_deleted: bool = False,
) -> DomainResource:
    domain = domain_service.get_domain(db, application, domain_id, include_deleted=include_deleted)
    return domain_resource(domain)


@router.post(
    "/domains/{domain_id}/checks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DomainResource,
    summary="Request a recheck",
    description=(
        "Asks the lifecycle worker to re-run ownership, routing, certificate and origin "
        "checks as soon as possible, for example after the customer fixed a DNS record. "
        "The response reflects the state before the recheck runs; poll the domain or "
        "subscribe to webhooks for the outcome.\n\n"
        "Manual rechecks are rate limited: at most one per domain every 60 seconds and "
        "60 per application per hour. Over the limit the response is `429 rate_limited` "
        "with a `Retry-After` header. A deleted domain cannot be rechecked and returns "
        "`409 invalid_status_transition`."
    ),
    responses={
        202: {"content": {"application/json": {"example": examples.WAITING_FOR_DNS}}},
        404: {"model": ErrorResponse},
        409: {
            "model": ErrorResponse,
            "description": "The domain is deleted and cannot be rechecked.",
            "content": {
                "application/json": {"example": examples.ERRORS["invalid_status_transition"]}
            },
        },
        429: {
            "model": ErrorResponse,
            "description": "Too many manual rechecks; wait for `Retry-After` seconds.",
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before retrying.",
                    "schema": {"type": "integer"},
                }
            },
            "content": {"application/json": {"example": examples.ERRORS["rate_limited"]}},
        },
    },
)
def request_recheck(
    domain_id: uuid.UUID, application: CurrentApplication, db: DbSession
) -> DomainResource:
    domain = domain_service.request_recheck(db, application, domain_id)
    db.commit()
    return domain_resource(domain)


@router.delete(
    "/domains/{domain_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DomainResource,
    summary="Delete a domain",
    description=(
        "Stops serving the hostname and revokes its ownership claim. The domain enters "
        "`deleting`, is excluded from listings, and its hostname can be registered again "
        "immediately (with new DNS records). Deleting an already deleted domain is a no-op."
    ),
    responses={
        202: {"content": {"application/json": {"example": examples.DELETED}}},
        404: {"model": ErrorResponse},
    },
)
def delete_domain(
    domain_id: uuid.UUID,
    application: CurrentApplication,
    db: DbSession,
    request: Request,
    background: BackgroundTasks,
) -> DomainResource:
    domain = domain_service.delete_domain(db, application, domain_id)
    db.commit()
    # Stop serving promptly instead of waiting for the next timer tick.
    reconciler = getattr(request.app.state, "reconciler", None)
    if reconciler is not None:
        background.add_task(reconciler.run_once)
    return domain_resource(domain)
