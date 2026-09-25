"""Canonical form and structural validation for customer hostnames.

Every hostname the service stores or compares goes through ``canonicalize``
first, so ``Forms.Example.com.`` and ``forms.example.com`` are the same claim.
The rules here need no external data. Public-suffix based apex detection and
reserved-name policy belong to the hardening work tracked in issue #12.
"""

from __future__ import annotations

import ipaddress
import re

import idna

MAX_HOSTNAME_LENGTH = 253
MAX_LABEL_LENGTH = 63
# Exact customer subdomains only for the MVP. A two-label name is treated as an
# apex until public-suffix aware detection lands (#12).
MIN_LABELS_FOR_SUBDOMAIN = 3

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class InvalidHostname(ValueError):
    """Raised when a hostname cannot be used as a customer domain.

    ``code`` is stable and safe to expose through the API.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonicalize(raw: str | None, *, allow_apex: bool = False) -> str:
    """Return the canonical ASCII form of ``raw`` or raise ``InvalidHostname``.

    Canonical form is lowercase, IDNA (punycode) encoded, without a trailing
    dot and without surrounding whitespace.
    """
    if raw is None:
        raise InvalidHostname("empty_hostname", "Hostname is required")
    value = raw.strip()
    if not value:
        raise InvalidHostname("empty_hostname", "Hostname is required")
    value = value.removesuffix(".")
    if "*" in value:
        raise InvalidHostname("wildcard_not_supported", "Wildcard hostnames are not supported")
    if _looks_like_ip(value):
        raise InvalidHostname(
            "ip_literal_not_supported", "IP addresses cannot be used as hostnames"
        )
    # Punycode never shortens a name, so an over-long input is over-long
    # regardless of encoding. Check before IDNA so the code is stable.
    if len(value) > MAX_HOSTNAME_LENGTH:
        raise InvalidHostname(
            "hostname_too_long", f"Hostname exceeds {MAX_HOSTNAME_LENGTH} characters"
        )
    try:
        ascii_value = idna.encode(value, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        raise InvalidHostname("invalid_hostname", f"Hostname is not valid: {exc}") from exc
    ascii_value = ascii_value.lower()
    if len(ascii_value) > MAX_HOSTNAME_LENGTH:
        raise InvalidHostname(
            "hostname_too_long", f"Hostname exceeds {MAX_HOSTNAME_LENGTH} characters"
        )

    labels = ascii_value.split(".")
    for label in labels:
        if not label or len(label) > MAX_LABEL_LENGTH or not _LABEL_RE.match(label):
            raise InvalidHostname("invalid_label", f"Label {label!r} is not valid")
    if labels[-1].isdigit():
        raise InvalidHostname("invalid_hostname", "Top-level label cannot be numeric")
    if not allow_apex and len(labels) < MIN_LABELS_FOR_SUBDOMAIN:
        raise InvalidHostname(
            "apex_not_supported",
            "Only subdomains such as forms.example.com are supported; "
            "apex domains are not supported yet",
        )
    return ascii_value


def _looks_like_ip(value: str) -> bool:
    candidate = value.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True
