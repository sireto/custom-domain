from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error response from the API: ``status``, stable ``code``, ``message``, ``details``."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class AuthenticationError(ApiError):
    """401: missing, invalid, revoked or expired credential."""


class NotFoundError(ApiError):
    """404: no such resource in this application."""


class ConflictError(ApiError):
    """409: hostname already claimed, invalid status transition, request in progress."""


class ValidationError(ApiError):
    """422: invalid hostname, reference, body or idempotency key reuse."""


class RateLimitedError(ApiError):
    """429: too many manual rechecks; wait ``retry_after`` seconds."""

    def __init__(self, status: int, code: str, message: str, details=None, retry_after: int = 1):
        super().__init__(status, code, message, details)
        self.retry_after = retry_after


class ServerError(ApiError):
    """5xx."""


class TransportError(Exception):
    """The request never produced an HTTP response (connection error, timeout)."""


def error_for(status: int, code: str, message: str, details=None, retry_after: int | None = None):
    if status == 401:
        return AuthenticationError(status, code, message, details)
    if status == 404:
        return NotFoundError(status, code, message, details)
    if status == 409:
        return ConflictError(status, code, message, details)
    if status == 422:
        return ValidationError(status, code, message, details)
    if status == 429:
        return RateLimitedError(status, code, message, details, retry_after or 1)
    if status >= 500:
        return ServerError(status, code, message, details)
    return ApiError(status, code, message, details)
