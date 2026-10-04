from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models import Application
from app.services.applications import authenticate_credential
from app.services.errors import InvalidCredential, RateLimited
from app.v1.errors import unauthorized
from app.v1.throttle import limited_client

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="ApplicationCredential",
    description=(
        "Application credential issued by the operator with "
        "`custom-domain credential issue`. Send it as `Authorization: Bearer cd_...`. "
        "The application is derived from the credential; it is never taken from the request."
    ),
)

DbSession = Annotated[Session, Depends(get_session)]


def current_application(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Application:
    limiter = getattr(request.app.state, "v1_auth_limiter", None)
    client = limited_client(request) if limiter is not None and limiter.enabled else None
    if client is not None:
        wait = limiter.retry_after(client)
        if wait:
            # Refused before any hashing or database work (app/v1/throttle.py).
            raise RateLimited(
                "Too many requests with an invalid credential from this address; retry later",
                retry_after=wait,
            )
    if credentials is None or credentials.scheme.lower() != "bearer":
        if client is not None:
            limiter.record_failure(client)
        raise unauthorized()
    try:
        credential = authenticate_credential(db, credentials.credentials)
    except InvalidCredential as exc:
        if client is not None:
            limiter.record_failure(client)
        raise unauthorized() from exc
    # last_used_at was updated by authenticate_credential; persist it now so a
    # failing request body does not discard it.
    db.commit()
    rate = getattr(request.app.state, "v1_rate_limiter", None)
    if rate is not None and rate.enabled:
        wait = rate.take(str(credential.id))
        if wait:
            raise RateLimited(
                f"More than {rate.per_minute} requests a minute with this credential; retry later",
                retry_after=wait,
            )
    # For the access log (app.main): which credential made the call, by id.
    # Not the key prefix: it starts with cd_ and the log redactor masks it.
    request.state.credential_id = str(credential.id)
    request.state.application_slug = credential.application.slug
    return credential.application


CurrentApplication = Annotated[Application, Depends(current_application)]
