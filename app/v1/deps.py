from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models import Application
from app.services.applications import authenticate_credential
from app.services.errors import InvalidCredential
from app.v1.errors import unauthorized

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
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Application:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()
    try:
        credential = authenticate_credential(db, credentials.credentials)
    except InvalidCredential as exc:
        raise unauthorized() from exc
    # last_used_at was updated by authenticate_credential; persist it now so a
    # failing request body does not discard it.
    db.commit()
    return credential.application


CurrentApplication = Annotated[Application, Depends(current_application)]
