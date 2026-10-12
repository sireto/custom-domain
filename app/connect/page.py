"""The customer's setup page: ``GET /connect/<token>`` and its two actions.

Served by this service on its edge hostname (the edge routes ``/connect/*``
to the API with ``/v1`` when ``PUBLIC_API`` is on, app/edge/config.py), so on
a dedicated edge it is under the operator's own name. The token in the path
names one connect session (app/services/connect_sessions.py) and is the only
credential: the page shows that domain alone, and never the application's
key, its other domains or operator detail.

* **No JavaScript.** A meta refresh every 30 seconds while setup is under
  way; values are selected to copy with ``user-select: all``.
* **Headers:** a strict CSP (no scripts, inline styles only, forms to this
  page and, for one-step setup, the provider's origin, which the apply
  redirect goes to), ``no-referrer`` so the token never leaves in a Referer,
  ``noindex``, no framing, ``no-store``.
* **POSTs** (``check`` and ``apply``) need the CSRF value of a cookie scoped
  to ``/connect/<token>`` (``SameSite=Strict``, ``HttpOnly``) repeated in
  the form: holding the link is not enough to make another browser act.
* **Not found** is one page and one status for a wrong, expired or revoked
  token, a deleted domain and an application that is no longer active. Each
  counts against the client's address like an invalid API key
  (``V1_AUTH_FAILURES_PER_MINUTE``), so guessing tokens is refused early.
* **Notices** after an action are fixed messages picked by a code.
"""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models import Application
from app.portal import presenters
from app.services import connect_sessions, domain_connect
from app.services import domains as domain_service
from app.services.errors import (
    DomainConnectSignerUnavailable,
    DomainConnectUnavailable,
    DomainNotFound,
    InvalidStatusTransition,
    RateLimited,
)
from app.v1.throttle import limited_client

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix=connect_sessions.PAGE_PATH, include_in_schema=False)

CSRF_COOKIE = "cd_connect"
REFRESH_SECONDS = 30
_CSRF = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ORIGIN_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")
_TOKEN_MAX = 64

DbSession = Annotated[Session, Depends(get_session)]


def headers(form_origin: str | None = None) -> dict[str, str]:
    form_action = "'self'" + (f" {form_origin}" if form_origin else "")
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action {form_action}; base-uri 'none'; frame-ancestors 'none'",
    }


def _edge_settings(request: Request):
    return getattr(request.app.state, "edge_settings", None)


def _brand(request: Request) -> str | None:
    """The edge hostname: the only name the page shows for the service."""
    settings = _edge_settings(request)
    return settings.edge_hostname if settings is not None else None


def _via_https(request: Request) -> bool:
    from app.portal.views import _via_https as via_https

    return via_https(request)


def _not_found(request: Request) -> HTMLResponse:
    response = templates.TemplateResponse(
        request, "not_found.html", {"brand": _brand(request)}, status_code=404
    )
    response.headers.update(headers())
    return response


def _refused(request: Request, token: str) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "refused.html",
        {"brand": _brand(request), "page": f"{connect_sessions.PAGE_PATH}/{token}"},
        status_code=403,
    )
    response.headers.update(headers())
    return response


def _back(token: str, notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"{connect_sessions.PAGE_PATH}/{token}?n={notice}",
        status_code=303,
        headers=headers(),
    )


def _too_many(request: Request, wait: int) -> HTMLResponse:
    response = templates.TemplateResponse(
        request, "too_many.html", {"brand": _brand(request)}, status_code=429
    )
    response.headers.update(headers())
    response.headers["Retry-After"] = str(wait)
    return response


def _open(request: Request, db: Session, token: str) -> connect_sessions.Opened | HTMLResponse:
    """The live session for ``token``, or the response to send instead.

    A link that does not work counts as a failed credential for the client's
    address, with the v1 API's limiter (``V1_AUTH_FAILURES_PER_MINUTE``): a
    client guessing tokens is refused before any lookup.
    """
    limiter = getattr(request.app.state, "v1_auth_limiter", None)
    client = limited_client(request) if limiter is not None and limiter.enabled else None
    if client is not None and (wait := limiter.retry_after(client)):
        return _too_many(request, wait)
    opened = connect_sessions.open_session(db, token) if len(token) <= _TOKEN_MAX else None
    if opened is None:
        if client is not None:
            limiter.record_failure(client)
        return _not_found(request)
    return opened


def _csrf_ok(request: Request, submitted: str) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    return bool(
        _CSRF.match(cookie) and _CSRF.match(submitted) and hmac.compare_digest(cookie, submitted)
    )


def _provider_origin(domain) -> str | None:
    """The https origin the apply URL goes to, for the CSP's form-action."""
    zone = domain.dns_zone if domain.zone else None
    url = (zone.domain_connect or {}).get("url_sync_ux") if zone is not None else None
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not _ORIGIN_HOST.match(parts.netloc or ""):
        return None
    return f"https://{parts.netloc}"


@router.get("/{token}")
def connect_page(
    request: Request,
    token: str,
    db: DbSession,
    n: str | None = None,
    error: str | None = None,
):
    opened = _open(request, db, token)
    if not isinstance(opened, connect_sessions.Opened):
        return opened
    application = db.get(Application, opened.application_id)
    try:
        domain = domain_service.get_domain(db, application, opened.domain_id)
    except DomainNotFound:
        return _not_found(request)
    view = presenters.connect_page_view(domain)
    code = "declined" if error == "access_denied" else n
    notice = presenters.CONNECT_NOTICES.get(code or "")
    csrf = request.cookies.get(CSRF_COOKIE) or ""
    if not _CSRF.match(csrf):
        csrf = secrets.token_urlsafe(32)
    page = f"{connect_sessions.PAGE_PATH}/{token}"
    form_origin = _provider_origin(domain) if view.can_apply else None
    response = templates.TemplateResponse(
        request,
        "connect.html",
        {
            "brand": _brand(request),
            "view": view,
            "notice": notice,
            "csrf": csrf,
            "page": page,
            "refresh": REFRESH_SECONDS if view.waiting else None,
            "expires_at": opened.expires_at,
            "app_name": opened.app_name,
            "return_url": opened.return_url,
        },
    )
    response.headers.update(headers(form_origin))
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        path=page,
        httponly=True,
        samesite="strict",
        secure=_via_https(request),
    )
    return response


@router.post("/{token}/check")
def check_now(
    request: Request,
    token: str,
    db: DbSession,
    csrf: Annotated[str, Form(max_length=64)] = "",
):
    opened = _open(request, db, token)
    if not isinstance(opened, connect_sessions.Opened):
        return opened
    if not _csrf_ok(request, csrf):
        return _refused(request, token)
    application_id, domain_id = opened.application_id, opened.domain_id
    # request_recheck takes the application lock as the first statement of
    # its transaction; end the read transaction above first.
    db.rollback()
    domain_service.lock_application(db, application_id)
    application = db.get(Application, application_id)
    try:
        domain_service.request_recheck(db, application, domain_id, requested_by="connect_page")
    except RateLimited:
        db.rollback()
        return _back(token, "rate_limited")
    except (DomainNotFound, InvalidStatusTransition):
        db.rollback()
        return _not_found(request)
    db.commit()
    return _back(token, "checking")


@router.post("/{token}/apply")
def apply(
    request: Request,
    token: str,
    db: DbSession,
    csrf: Annotated[str, Form(max_length=64)] = "",
):
    opened = _open(request, db, token)
    if not isinstance(opened, connect_sessions.Opened):
        return opened
    if not _csrf_ok(request, csrf):
        return _refused(request, token)
    application = db.get(Application, opened.application_id)
    base = connect_sessions.page_base_url(_edge_settings(request))
    # The provider sends the customer back through the service's return
    # address; the page's own URL (it holds the token) travels encrypted in
    # the sealed state, and is never sent to the provider in the clear.
    back = f"{connect_sessions.page_url(base, token)}?n=returned" if base else None
    try:
        plan = domain_connect.prepare_connect(
            db,
            application,
            opened.domain_id,
            redirect_uri=back,
            private_redirect=True,
            return_url=domain_connect.default_return_url(_edge_settings(request)),
        )
        # No transaction is open while signing (see the v1 connect endpoint).
        db.rollback()
        url = domain_connect.sign_connect(
            plan, transport=getattr(request.app.state, "domain_connect_transport", None)
        )
        issued = domain_connect.record_connect(db, application, plan, url)
        db.commit()
    except DomainNotFound:
        db.rollback()
        return _not_found(request)
    except DomainConnectUnavailable:
        db.rollback()
        return _back(token, "unavailable")
    except DomainConnectSignerUnavailable:
        db.rollback()
        return _back(token, "signer")
    except RateLimited:
        db.rollback()
        return _back(token, "busy")
    return RedirectResponse(issued.url, status_code=303, headers=headers())
