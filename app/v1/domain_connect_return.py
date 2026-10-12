"""Where DNS providers send the customer back after a Domain Connect apply.

``GET /v1/domain-connect/return?state=<sealed>[&error=...]`` is opened by the
customer's browser, so it takes no credential: the sealed state
(app/services/secret_box.py) is what names the domain, and only this service
can make one. The redirect is never trusted: it only makes the domain's DNS
checks due now; the checks decide what happened. The customer then goes on
to the application's own ``redirect_uri`` (sealed with the state, so this is
not an open redirect) with the application's ``state``, or sees a short page
when the application gave none.

Not part of the documented API contract (it is not in the OpenAPI document);
docs/domain-connect.md describes it.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services import domain_connect

router = APIRouter(prefix="/v1/domain-connect", include_in_schema=False)

# The sealed state is in this page's URL: never send it on as a Referer, and
# never let the page be framed or run anything.
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}
_ERROR = re.compile(r"^[a-z_]{1,64}$")

_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body><main><h1>{title}</h1><p>{text}</p></main></body>
</html>
"""


def _page(title: str, text: str, status: int) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=title, text=text), status_code=status, headers=HEADERS)


@router.get("/return")
def domain_connect_return(
    db: Annotated[Session, Depends(get_session)],
    state: Annotated[str, Query(max_length=4096)] = "",
    error: Annotated[str | None, Query(max_length=64)] = None,
):
    opened = domain_connect.open_return_state(state) if state else None
    if opened is None:
        return _page(
            "Link not valid",
            "This link was not issued by this service. Return to the application you came from.",
            400,
        )
    if opened.expired:
        return _page(
            "Link expired",
            "This link has expired. Return to the application you came from; the DNS "
            "records are checked regularly anyway.",
            400,
        )
    if domain_connect.connect_returned(db, opened.domain_id):
        db.commit()
    error = error if error is not None and _ERROR.match(error) else None
    if opened.redirect_uri:
        target = domain_connect.back_to_application(opened.redirect_uri, opened.state, error)
        return RedirectResponse(target, status_code=303, headers=HEADERS)
    if error is not None:
        return _page(
            "No changes made",
            "Your DNS provider made no changes. You can close this window.",
            200,
        )
    return _page(
        "Thank you",
        "Your DNS provider is applying the records. The domain is checked again now; you "
        "can close this window.",
        200,
    )
