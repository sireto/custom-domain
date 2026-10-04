"""Rotating the operator API token without editing .env or restarting.

``OPERATOR_API_TOKEN`` turns the operator API on and is its first token. A
caller holding it can set a new one with ``PUT /operator/v1/token``: its
SHA-256 is stored, and from then on only the new token is accepted, by
every API instance (each request reads the stored hash). The environment
token stops working, so a token that was handed out at install time (in
cloud-init user data, for example) is worthless after the first rotation.

``custom-domain operator reset-token`` deletes the stored hash, so the
environment token works again: the way back for an operator with shell
access who lost the rotated token.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from sqlalchemy.orm import Session

from app.edge.settings import MIN_OPERATOR_TOKEN_LENGTH
from app.models import OperatorToken
from app.models.types import utcnow
from app.services.errors import ServiceError

ACTIVE = "active"
MAX_LENGTH = 256
# Plain characters only, the same set the installer accepts in .env.
ALLOWED = re.compile(r"[A-Za-z0-9._~+/=-]+")


class InvalidOperatorToken(ServiceError):
    code = "invalid_operator_token"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def active(session: Session) -> OperatorToken | None:
    return session.get(OperatorToken, ACTIVE)


def matches(session: Session, given: str, env_token: str) -> bool:
    """Whether ``given`` is the current token: the rotated one if set, else the env one."""
    row = active(session)
    if row is not None:
        return hmac.compare_digest(digest(given), row.token_hash)
    return hmac.compare_digest(given.encode(), env_token.encode())


def set_token(session: Session, token: str) -> OperatorToken:
    if not (MIN_OPERATOR_TOKEN_LENGTH <= len(token) <= MAX_LENGTH) or not ALLOWED.fullmatch(token):
        raise InvalidOperatorToken(
            f"The token must be {MIN_OPERATOR_TOKEN_LENGTH} to {MAX_LENGTH} characters of "
            "letters, digits and . _ ~ + / = -"
        )
    row = active(session)
    if row is None:
        row = OperatorToken(name=ACTIVE, token_hash=digest(token), set_at=utcnow())
        session.add(row)
    else:
        row.token_hash = digest(token)
        row.set_at = utcnow()
    session.flush()
    return row


def reset(session: Session) -> bool:
    row = active(session)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
