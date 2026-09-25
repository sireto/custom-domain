"""Error envelope and the mapping from service errors to HTTP responses.

Every v1 error body has the shape ``{"error": {"code", "message", "details"}}``.
Codes are stable and documented in docs/api-v1.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.hostname import InvalidHostname
from app.services.errors import (
    ApplicationSuspended,
    DomainNotFound,
    HostnameAlreadyClaimed,
    InvalidCredential,
    InvalidReference,
    InvalidStatusTransition,
    RateLimited,
    ServiceError,
)
from app.services.idempotency import IdempotencyInProgress, IdempotencyKeyReused


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


def unauthorized(message: str = "A valid application credential is required") -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


SERVICE_ERROR_STATUS: list[tuple[type[ServiceError], int]] = [
    (InvalidCredential, 401),
    (ApplicationSuspended, 403),
    (DomainNotFound, 404),
    (HostnameAlreadyClaimed, 409),
    (InvalidStatusTransition, 409),
    (IdempotencyInProgress, 409),
    (IdempotencyKeyReused, 422),
    (InvalidReference, 422),
    (RateLimited, 429),
]


def _status_for(exc: ServiceError) -> int:
    for cls, status in SERVICE_ERROR_STATUS:
        if isinstance(exc, cls):
            return status
    return 400


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body), headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(InvalidHostname)
    async def _invalid_hostname(_request: Request, exc: InvalidHostname) -> JSONResponse:
        return error_response(422, exc.code, exc.message, {"field": "hostname"})

    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        headers: dict[str, str] | None = None
        if isinstance(exc, InvalidCredential):
            headers = {"WWW-Authenticate": "Bearer"}
        elif isinstance(exc, RateLimited):
            headers = {"Retry-After": str(exc.retry_after)}
        return error_response(_status_for(exc), exc.code, exc.message, exc.details, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "location": list(err.get("loc", ())),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return error_response(422, "validation_error", "Request is not valid", {"errors": errors})
