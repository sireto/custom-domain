"""Stable, machine-readable errors raised by the service layer.

The API layer (#1) maps ``code`` to HTTP responses; nothing here leaks
another application's existence.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    code = "service_error"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}


class InvalidApplication(ServiceError):
    code = "invalid_application"


class ApplicationAlreadyExists(ServiceError):
    code = "application_already_exists"


class ApplicationNotFound(ServiceError):
    code = "application_not_found"


class ApplicationSuspended(ServiceError):
    code = "application_suspended"


class HostnameAlreadyClaimed(ServiceError):
    code = "hostname_already_claimed"


class DomainNotFound(ServiceError):
    code = "domain_not_found"


class InvalidReference(ServiceError):
    code = "invalid_reference"


class InvalidStatusTransition(ServiceError):
    code = "invalid_status_transition"


class InvalidCredential(ServiceError):
    code = "invalid_credential"


class CredentialNotFound(ServiceError):
    code = "credential_not_found"


class InvalidOrigin(ServiceError):
    code = "invalid_origin"


class OriginConflict(ServiceError):
    code = "origin_conflict"


class OriginNotVerified(ServiceError):
    code = "origin_not_verified"
