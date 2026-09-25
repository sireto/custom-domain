"""Custom Domain SDK.

* :class:`Client` talks to the v1 API.
* :func:`verify_assertion` and :class:`WorkspaceResolver` verify the signed
  workspace assertion the edge adds to every proxied request.
* :func:`verify_webhook` and :func:`parse_event` handle webhook deliveries.
* :class:`CustomDomainMiddleware` does both for ASGI applications and serves
  the workspace probe.
"""

from custom_domain.assertion import Assertion, AssertionInvalid, verify_assertion
from custom_domain.client import Client
from custom_domain.errors import (
    ApiError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ServerError,
    TransportError,
    ValidationError,
)
from custom_domain.middleware import CustomDomainMiddleware, WorkspaceResolver
from custom_domain.models import (
    Check,
    Delivery,
    DnsRecord,
    Domain,
    Page,
    Webhook,
    WebhookEvent,
)
from custom_domain.webhooks import SignatureInvalid, parse_event, verify_webhook

__all__ = [
    "ApiError",
    "Assertion",
    "AssertionInvalid",
    "AuthenticationError",
    "Check",
    "Client",
    "ConflictError",
    "CustomDomainMiddleware",
    "Delivery",
    "DnsRecord",
    "Domain",
    "NotFoundError",
    "Page",
    "RateLimitedError",
    "ServerError",
    "SignatureInvalid",
    "TransportError",
    "ValidationError",
    "Webhook",
    "WebhookEvent",
    "WorkspaceResolver",
    "parse_event",
    "verify_assertion",
    "verify_webhook",
]

__version__ = "0.1.0"
