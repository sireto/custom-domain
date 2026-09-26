"""Portal pages: the operator actions of the ``custom-domain`` command in a browser.

Every action calls the same service function the command line does, so the
two stay equivalent. Secrets (API keys, webhook signing secrets) are rendered
once, in the response to the action that produced them, and never stored in
the session. What statuses mean is decided in ``presenters``.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.db.session import get_session
from app.hostname import InvalidHostname
from app.legacy import import_legacy_domains, parse_legacy_config
from app.models import ApplicationStatus, DomainStatus
from app.portal import presenters
from app.portal.auth import LoginLimiter, PortalSettings, Sessions, safe_next
from app.services import applications as app_service
from app.services import domains as domain_service
from app.services.errors import ServiceError
from app.services.origin_verification import (
    OriginVerificationFailed,
    allow_private_from_env,
    verify_origin,
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/portal", include_in_schema=False)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'",
}


class RequireLogin(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


class PortalDisabled(Exception):
    pass


def _state(request: Request) -> tuple[PortalSettings, Sessions, LoginLimiter]:
    state = request.app.state
    return state.portal_settings, state.portal_sessions, state.portal_limiter


def client_address(request: Request) -> str | None:
    """The address a portal request is attributed to.

    Through the edge (a trusted peer, see EDGE_ASK_TRUSTED_HOSTS) it is the
    last X-Forwarded-For value, which Caddy sets to the real peer; otherwise
    the connecting address itself. The sign-in rate limit and the allowlist
    both use it.
    """
    peer = request.client.host if request.client else None
    edge_settings = getattr(request.app.state, "edge_settings", None)
    forwarded = request.headers.get("x-forwarded-for", "")
    if peer and forwarded and edge_settings is not None and edge_settings.trusts(peer):
        return forwarded.split(",")[-1].strip() or peer
    return peer


def _via_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    edge_settings = getattr(request.app.state, "edge_settings", None)
    return bool(
        peer
        and edge_settings is not None
        and edge_settings.trusts(peer)
        and request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


class AddressRefused(Exception):
    pass


def _check_address(request: Request) -> str:
    address = client_address(request)
    edge_settings = getattr(request.app.state, "edge_settings", None)
    if edge_settings is None:
        import ipaddress

        try:
            allowed = not ipaddress.ip_address(address or "").is_global
        except ValueError:
            allowed = address == "testclient"
    else:
        allowed = edge_settings.portal_allows(address) or address == "testclient"
    if not allowed:
        raise AddressRefused()
    return address or "unknown"


def _session(request: Request) -> dict:
    settings, sessions, _ = _state(request)
    if not settings.enabled:
        raise PortalDisabled()
    _check_address(request)
    data = sessions.read(request.cookies.get("cd_portal"))
    if data is None:
        raise RequireLogin(request.url.path)
    return data


def _csrf(request: Request, session: dict, token: str) -> None:
    import hmac

    if not token or not hmac.compare_digest(token, session["csrf"]):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


Operator = Depends(_session)
DbSession = Depends(get_session)


def render(request: Request, name: str, session: dict | None = None, status: int = 200, **ctx: Any):
    context = {"request": request, "csrf": session["csrf"] if session else None, **ctx}
    response = templates.TemplateResponse(request, name, context, status_code=status)
    response.headers.update(SECURITY_HEADERS)
    return response


def redirect(url: str, status: int = 303) -> RedirectResponse:
    response = RedirectResponse(url, status_code=status)
    response.headers.update(SECURITY_HEADERS)
    return response


def install(app) -> None:
    """Mount the portal and its sign-in redirect on ``app``."""
    from starlette.requests import Request as _Request

    settings = PortalSettings.from_env()
    app.state.portal_settings = settings
    app.state.portal_sessions = Sessions(settings)
    app.state.portal_limiter = LoginLimiter()
    app.include_router(router)

    @app.exception_handler(RequireLogin)
    async def _require_login(_request: _Request, exc: RequireLogin):
        return redirect(f"/portal/login?next={safe_next(exc.next_path)}")

    @app.exception_handler(PortalDisabled)
    async def _disabled(request: _Request, _exc: PortalDisabled):
        return render(request, "disabled.html", status=503)

    @app.exception_handler(AddressRefused)
    async def _refused(request: _Request, _exc: AddressRefused):
        return render(request, "refused.html", status=403, address=client_address(request))


# --- sign in -------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, next: str = "/portal"):
    settings, sessions, _ = _state(request)
    if not settings.enabled:
        raise PortalDisabled()
    _check_address(request)
    if sessions.read(request.cookies.get("cd_portal")) is not None:
        return redirect(safe_next(next))
    return render(request, "login.html", next=safe_next(next))


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/portal")):
    settings, sessions, limiter = _state(request)
    if not settings.enabled:
        raise PortalDisabled()
    client = _check_address(request)
    if not limiter.allowed(client):
        return render(
            request,
            "login.html",
            status=429,
            next=safe_next(next),
            error="Too many failed sign-ins; try again in a few minutes.",
        )
    if not settings.check_password(password):
        limiter.record_failure(client)
        return render(
            request, "login.html", status=401, next=safe_next(next), error="Wrong password."
        )
    limiter.reset(client)
    value, _ = sessions.issue()
    response = redirect(safe_next(next))
    sessions.set_cookie(response, value, secure=_via_https(request))
    return response


@router.post("/logout")
def logout(request: Request, session: dict = Operator, csrf: str = Form("")):
    _csrf(request, session, csrf)
    _, sessions, _ = _state(request)
    response = redirect("/portal/login")
    sessions.clear_cookie(response)
    return response


# --- notices -----------------------------------------------------------------------

# Redirects carry a notice code, never free text, so a crafted link cannot put
# arbitrary words on a portal page.
NOTICES = {
    "application_created": "Application created. Follow the setup steps below to start "
    "serving hostnames.",
    "application_deleted": "Application deleted. Its hostnames are no longer served; their "
    "records and history are kept for 90 days, then purged.",
    "renamed": "Name saved.",
    "cname_target": "CNAME target saved. New domains get the new target.",
    "cname_target_reissued": "CNAME target saved and the DNS records of existing domains "
    "re-issued. Those domains wait for DNS until their customers publish the new records.",
    "probe": "Readiness setting saved.",
    "suspended": "Application suspended. Its hostnames are no longer served and its API keys "
    "are refused.",
    "activated": "Application active again.",
    "origin_registered": "Origin registered. Serve the token shown below from it, then verify.",
    "origin_verified": "Origin verified and active. The edge sends this application's traffic "
    "there.",
    "origin_verified_only": "Origin verified. Activate it to send traffic there.",
    "origin_activated": "Origin activated. The edge sends this application's traffic there.",
    "origin_retired": "Origin retired. It no longer receives traffic.",
    "origin_deleted": "Origin deleted.",
    "credential_revoked": "API key revoked. Requests using it are refused from now on.",
    "credential_deleted": "API key deleted.",
    "domain_registered": "Hostname registered. Give the customer the two DNS records below.",
    "domain_recheck": "Recheck requested. The worker runs every check within a minute.",
    "domain_reissue": "New DNS records issued. The customer must publish them; the old ones "
    "no longer verify.",
    "domain_delete": "Hostname deleted. It is no longer served.",
    "webhook_revoked": "Webhook revoked. Nothing more is delivered to it.",
    "webhook_deleted": "Webhook deleted.",
    "replayed": "Delivery queued again.",
    "purged": "Deleted hostnames and applications past the 90-day retention purged.",
    "reconcile_applied": "Edge configuration applied.",
    "reconcile_unchanged": "The edge already runs the desired configuration.",
}


def _notice(code: str) -> str | None:
    return NOTICES.get(code)


def _edge_settings(request: Request):
    return getattr(request.app.state, "edge_settings", None)


def _resolver(request: Request):
    from app.services.doctor import _resolve

    return getattr(request.app.state, "portal_resolve", None) or _resolve


def _prober(request: Request):
    from app.services.doctor import _probe_target

    return getattr(request.app.state, "portal_probe", None) or _probe_target


def _dns_settings():
    from app.dns.settings import DnsSettings

    return DnsSettings.from_env()


def _domain_counts(db: Session, application=None) -> dict[DomainStatus, int]:
    from sqlalchemy import func, select

    from app.models import Domain

    query = select(Domain.status, func.count()).group_by(Domain.status)
    if application is not None:
        query = query.where(Domain.application_id == application.id)
    counts = {status: 0 for status in DomainStatus}
    for status, n in db.execute(query).all():
        counts[status] = n
    return counts


def _live(counts: dict[DomainStatus, int]) -> int:
    return sum(n for s, n in counts.items() if s != DomainStatus.DELETING)


# --- overview ----------------------------------------------------------------------


@router.get("")
@router.get("/")
def dashboard(request: Request, session: dict = Operator, db: Session = DbSession, ok: str = ""):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.models import Domain, DomainEvent, EdgeLock

    applications = app_service.list_applications(db)
    counts = _domain_counts(db)
    attention = list(
        db.scalars(
            select(Domain)
            .options(joinedload(Domain.application))
            .where(
                Domain.deleted_at.is_(None),
                Domain.status.in_([DomainStatus.ATTENTION_REQUIRED, DomainStatus.SUSPENDED]),
            )
            .order_by(Domain.updated_at.desc())
            .limit(8)
        )
    )
    issues = []
    for application in applications:
        if application.status != ApplicationStatus.ACTIVE:
            issues.append((application, "Suspended: none of its hostnames is served."))
        elif application.serving_origin is None:
            issues.append(
                (application, "No verified, active origin: its hostnames cannot go live.")
            )
    recent = list(
        db.execute(
            select(DomainEvent, Domain.hostname, Domain.id, Domain.application_id)
            .join(Domain, Domain.id == DomainEvent.domain_id)
            .where(DomainEvent.event_type != "domain.check_updated")
            .order_by(DomainEvent.created_at.desc())
            .limit(10)
        ).all()
    )
    slugs = {a.id: a for a in applications}
    lock = db.scalar(select(EdgeLock).where(EdgeLock.name == "reconcile"))
    reconciler = getattr(request.app.state, "reconciler", None)
    return render(
        request,
        "dashboard.html",
        session,
        section="overview",
        applications=applications,
        stats={
            "live": counts[DomainStatus.READY],
            "waiting": counts[DomainStatus.PENDING_DNS] + counts[DomainStatus.PROVISIONING],
            "attention": counts[DomainStatus.ATTENTION_REQUIRED] + counts[DomainStatus.SUSPENDED],
        },
        attention=attention,
        issues=issues,
        recent=[
            (event, hostname, domain_id, slugs.get(app_id))
            for event, hostname, domain_id, app_id in recent
        ],
        lock=lock,
        edge_settings=_edge_settings(request),
        last_reconcile=reconciler.last_result if reconciler else None,
        notice=_notice(ok),
        p=presenters,
    )


@router.post("/maintenance/purge-tombstones")
def purge_tombstones(
    request: Request, session: dict = Operator, db: Session = DbSession, csrf: str = Form("")
):
    _csrf(request, session, csrf)
    domain_service.purge_tombstones(db)
    db.commit()
    return redirect("/portal?ok=purged")


# --- applications ------------------------------------------------------------------


@router.get("/applications")
def applications(request: Request, session: dict = Operator, db: Session = DbSession, ok: str = ""):
    from sqlalchemy import func, select

    from app.models import Domain

    rows = db.execute(
        select(Domain.application_id, Domain.status, func.count())
        .where(Domain.deleted_at.is_(None))
        .group_by(Domain.application_id, Domain.status)
    ).all()
    per_app: dict = {}
    for app_id, status, n in rows:
        per_app.setdefault(app_id, {})[status] = n
    return render(
        request,
        "applications.html",
        session,
        section="applications",
        applications=app_service.list_applications(db),
        per_app=per_app,
        notice=_notice(ok),
        p=presenters,
        DomainStatus=DomainStatus,
    )


@router.get("/new-application")
def new_application(request: Request, session: dict = Operator):
    settings = _edge_settings(request)
    return render(
        request,
        "application_new.html",
        session,
        section="applications",
        form={
            "cname_target": settings.edge_hostname if settings and settings.edge_hostname else ""
        },
        edge_hostname=settings.edge_hostname if settings else None,
    )


@router.post("/applications")
def create_application(
    request: Request,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    slug: str = Form(""),
    name: str = Form(""),
    cname_target: str = Form(""),
):
    _csrf(request, session, csrf)
    try:
        application = app_service.create_application(
            db, slug=slug.strip(), name=name, cname_target=cname_target
        )
        db.commit()
    except ServiceError as exc:
        db.rollback()
        settings = _edge_settings(request)
        return render(
            request,
            "application_new.html",
            session,
            status=400,
            section="applications",
            error=exc.message,
            form={"slug": slug, "name": name, "cname_target": cname_target},
            edge_hostname=settings.edge_hostname if settings else None,
        )
    return redirect(f"/portal/applications/{application.slug}?ok=application_created")


def _load_application(db: Session, slug: str):
    try:
        return app_service.get_application_by_slug(db, slug)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _app_context(db: Session, application) -> dict[str, Any]:
    counts = _domain_counts(db, application)
    return {
        "section": "applications",
        "application": application,
        "app_state": presenters.application_state(application),
        "tab_counts": {"domains": _live(counts)},
        "domain_counts": counts,
        "p": presenters,
    }


def _tab_overview(request, session, db, application, *, status=200, **extra):
    origins = app_service.list_origins(db, application)
    credentials = app_service.list_credentials(db, application)
    ctx = _app_context(db, application)
    from app.services.doctor import check_edge_name

    target = presenters.EdgeNameView(application.cname_target, [])
    dns_settings = _dns_settings()
    resolve = _resolver(request)
    presenters.resolve_names([target], resolve, dns_settings)
    settings = _edge_settings(request)
    if target.addresses and not target.error and settings is not None:
        # The setup step is done only when the name reaches this edge, not
        # merely when it resolves somewhere.
        target.reachability = check_edge_name(
            target.name, settings, dns_settings, resolve=resolve, probe_target=_prober(request)
        )
    steps = presenters.application_steps(
        application,
        origins=origins,
        credentials=credentials,
        domain_counts=ctx["domain_counts"],
        target_dns=target,
    )
    return render(
        request,
        "app_overview.html",
        session,
        status=status,
        steps=steps,
        done=sum(1 for s in steps if s.done),
        current=next((i for i, s in enumerate(steps) if not s.done), None),
        target=target,
        serving=application.serving_origin,
        **ctx,
        **extra,
    )


def _tab_domains(
    request, session, db, application, *, status=200, domain_status="", q="", offset=0, **extra
):
    wanted = None
    if domain_status:
        try:
            wanted = DomainStatus(domain_status)
        except ValueError:
            domain_status = ""
    offset = max(0, offset)
    domains, has_more = domain_service.page_domains(
        db,
        application,
        status=wanted,
        include_deleted=wanted == DomainStatus.DELETING,
        search=q,
        limit=50,
        offset=offset,
    )
    return render(
        request,
        "app_domains.html",
        session,
        status=status,
        domains=domains,
        has_more=has_more,
        offset=offset,
        domain_status=domain_status,
        q=q,
        filters=presenters.DOMAIN_FILTERS,
        **_app_context(db, application),
        **extra,
    )


def _tab_origins(request, session, db, application, *, status=200, **extra):
    from app.services.origin_verification import WELL_KNOWN_PATH

    return render(
        request,
        "app_origins.html",
        session,
        status=status,
        origins=app_service.list_origins(db, application),
        well_known=WELL_KNOWN_PATH,
        **_app_context(db, application),
        **extra,
    )


def _tab_credentials(request, session, db, application, *, status=200, **extra):
    return render(
        request,
        "app_credentials.html",
        session,
        status=status,
        credentials=app_service.list_credentials(db, application),
        **_app_context(db, application),
        **extra,
    )


def _tab_webhooks(request, session, db, application, *, status=200, **extra):
    from app.services import webhooks as webhook_service

    return render(
        request,
        "app_webhooks.html",
        session,
        status=status,
        subscriptions=webhook_service.list_subscriptions(db, application),
        event_types=webhook_service.WEBHOOK_EVENT_TYPES,
        **_app_context(db, application),
        **extra,
    )


def _tab_settings(request, session, db, application, *, status=200, **extra):
    return render(
        request,
        "app_settings.html",
        session,
        status=status,
        edge_names=app_service.edge_names(db, application),
        live_domains=app_service.live_domain_count(db, application),
        **_app_context(db, application),
        **extra,
    )


TABS = {
    "overview": _tab_overview,
    "domains": _tab_domains,
    "origins": _tab_origins,
    "credentials": _tab_credentials,
    "webhooks": _tab_webhooks,
    "settings": _tab_settings,
}


@router.get("/applications/{slug}")
def application(
    request: Request, slug: str, session: dict = Operator, db: Session = DbSession, ok: str = ""
):
    return _tab_overview(request, session, db, _load_application(db, slug), notice=_notice(ok))


@router.get("/applications/{slug}/domains")
def application_domains(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    ok: str = "",
    status: str = "",
    q: str = "",
    offset: int = 0,
):
    return _tab_domains(
        request,
        session,
        db,
        _load_application(db, slug),
        domain_status=status,
        q=q[:253],
        offset=offset,
        notice=_notice(ok),
    )


@router.get("/applications/{slug}/{tab}")
def application_tab(
    request: Request,
    slug: str,
    tab: str,
    session: dict = Operator,
    db: Session = DbSession,
    ok: str = "",
):
    if tab not in TABS or tab in ("overview", "domains"):
        raise HTTPException(status_code=404)
    return TABS[tab](request, session, db, _load_application(db, slug), notice=_notice(ok))


ACTION_ERRORS = (ServiceError, OriginVerificationFailed, InvalidHostname, ValueError)


def _action(request, session, db, slug, csrf, fn, *, tab: str, ok: str, then: str | None = None):
    """Run a service action; re-render the tab with the error, or redirect with a notice.

    ``fn`` may return a dict of values to render once on the tab (a secret),
    or a path to redirect to instead of the tab.
    """
    _csrf(request, session, csrf)
    application = _load_application(db, slug)
    try:
        result = fn(application)
        db.commit()
    except ACTION_ERRORS as exc:
        db.rollback()
        message = getattr(exc, "message", None) or str(exc)
        return TABS[tab](request, session, db, application, status=400, error=message)
    if isinstance(result, dict):
        return TABS[tab](request, session, db, application, **result)
    if isinstance(result, str):
        return redirect(f"{result}?ok={ok}")
    suffix = "" if tab == "overview" else f"/{tab}"
    return redirect(then or f"/portal/applications/{slug}{suffix}?ok={ok}")


# --- application settings ----------------------------------------------------------


@router.post("/applications/{slug}/name")
def rename_application(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    name: str = Form(""),
):
    def act(application):
        app_service.rename_application(db, application, name)

    return _action(request, session, db, slug, csrf, act, tab="settings", ok="renamed")


@router.post("/applications/{slug}/cname-target")
def set_cname_target(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    cname_target: str = Form(""),
    reissue_claims: bool = Form(False),
):
    def act(application):
        target = app_service.set_cname_target(db, application, cname_target)
        if reissue_claims:
            for domain in domain_service.list_domains(db, application, limit=10000):
                claim = domain.active_claim
                if claim is not None and claim.cname_target != target:
                    domain_service.reissue_claim(db, application, domain.id)

    ok = "cname_target_reissued" if reissue_claims else "cname_target"
    return _action(request, session, db, slug, csrf, act, tab="settings", ok=ok)


@router.post("/applications/{slug}/workspace-probe")
def set_workspace_probe(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    enabled: bool = Form(False),
):
    def act(application):
        application.workspace_probe_enabled = enabled

    return _action(request, session, db, slug, csrf, act, tab="settings", ok="probe")


@router.post("/applications/{slug}/status")
def set_status(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    status: str = Form(""),
):
    wanted = ApplicationStatus(status) if status in ApplicationStatus._value2member_map_ else None

    def act(application):
        if wanted is None:
            raise ValueError("Unknown status")
        app_service.set_application_status(db, application, wanted)

    ok = "suspended" if wanted == ApplicationStatus.SUSPENDED else "activated"
    return _action(request, session, db, slug, csrf, act, tab="settings", ok=ok)


@router.post("/applications/{slug}/delete")
def delete_application(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    confirm: str = Form(""),
    delete_domains: bool = Form(False),
):
    def act(application):
        app_service.delete_application(
            db, application, confirm_slug=confirm, delete_domains=delete_domains
        )
        return "/portal/applications"

    return _action(request, session, db, slug, csrf, act, tab="settings", ok="application_deleted")


# --- origins -----------------------------------------------------------------------


@router.post("/applications/{slug}/origins")
def register_origin(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    host: str = Form(""),
    scheme: str = Form("https"),
    port: int | None = Form(None),
):
    def act(application):
        app_service.register_origin(db, application, host=host, scheme=scheme, port=port)

    return _action(request, session, db, slug, csrf, act, tab="origins", ok="origin_registered")


def _origin(db, application, origin_id):
    return app_service.get_origin(db, application, origin_id=origin_id)


@router.post("/applications/{slug}/origins/{origin_id}/verify")
def verify_origin_view(
    request: Request,
    slug: str,
    origin_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    activate: bool = Form(False),
):
    def act(application):
        origin = _origin(db, application, origin_id)
        try:
            verify_origin(db, origin, allow_private=allow_private_from_env())
        except OriginVerificationFailed:
            db.commit()  # the failure is recorded on the origin
            raise
        if activate:
            app_service.activate_origin(db, origin)

    ok = "origin_verified" if activate else "origin_verified_only"
    return _action(request, session, db, slug, csrf, act, tab="origins", ok=ok)


@router.post("/applications/{slug}/origins/{origin_id}/activate")
def activate_origin_view(
    request: Request,
    slug: str,
    origin_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    def act(application):
        app_service.activate_origin(db, _origin(db, application, origin_id))

    return _action(request, session, db, slug, csrf, act, tab="origins", ok="origin_activated")


@router.post("/applications/{slug}/origins/{origin_id}/retire")
def retire_origin_view(
    request: Request,
    slug: str,
    origin_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    def act(application):
        app_service.retire_origin(db, _origin(db, application, origin_id))

    return _action(request, session, db, slug, csrf, act, tab="origins", ok="origin_retired")


@router.post("/applications/{slug}/origins/{origin_id}/delete")
def delete_origin_view(
    request: Request,
    slug: str,
    origin_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    def act(application):
        app_service.delete_origin(db, _origin(db, application, origin_id))

    return _action(request, session, db, slug, csrf, act, tab="origins", ok="origin_deleted")


# --- credentials --------------------------------------------------------------------


@router.post("/applications/{slug}/credentials")
def issue_credential(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    label: str = Form(""),
    expires_in_days: str = Form(""),
):
    def act(application):
        from app.models.types import utcnow

        days = int(expires_in_days) if expires_in_days.strip() else None
        if days is not None and days < 1:
            raise ValueError("Expiry must be at least one day, or empty for no expiry")
        expires_at = utcnow() + timedelta(days=days) if days else None
        credential, secret = app_service.issue_credential(
            db, application, label=label, expires_at=expires_at
        )
        return {"credential_secret": secret, "credential_label": credential.label}

    return _action(request, session, db, slug, csrf, act, tab="credentials", ok="")


@router.post("/applications/{slug}/credentials/{credential_id}/revoke")
def revoke_credential(
    request: Request,
    slug: str,
    credential_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    def act(application):
        app_service.revoke_credential(db, application, credential_id)

    return _action(
        request, session, db, slug, csrf, act, tab="credentials", ok="credential_revoked"
    )


@router.post("/applications/{slug}/credentials/{credential_id}/rotate")
def rotate_credential(
    request: Request,
    slug: str,
    credential_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    grace_hours: int = Form(24),
):
    def act(application):
        credential, secret, old = app_service.rotate_credential(
            db, application, credential_id, grace=timedelta(hours=max(0, grace_hours))
        )
        return {
            "credential_secret": secret,
            "credential_label": credential.label,
            "rotated_from": old,
        }

    return _action(request, session, db, slug, csrf, act, tab="credentials", ok="")


@router.post("/applications/{slug}/credentials/{credential_id}/delete")
def delete_credential(
    request: Request,
    slug: str,
    credential_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    def act(application):
        app_service.delete_credential(db, application, credential_id)

    return _action(
        request, session, db, slug, csrf, act, tab="credentials", ok="credential_deleted"
    )


# --- webhooks -----------------------------------------------------------------------


@router.post("/applications/{slug}/webhooks")
def create_webhook(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    url: str = Form(""),
    events: list[str] = Form([]),
):
    from app.services import webhooks as webhook_service

    def act(application):
        subscription, secret = webhook_service.create_subscription(
            db,
            application,
            url=url,
            events=events,
            allow_private=webhook_service.allow_private_from_env(),
        )
        return {"webhook_secret": secret, "webhook_url": subscription.url}

    return _action(request, session, db, slug, csrf, act, tab="webhooks", ok="")


@router.post("/applications/{slug}/webhooks/{subscription_id}/rotate")
def rotate_webhook(
    request: Request,
    slug: str,
    subscription_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    from app.services import webhooks as webhook_service

    def act(application):
        subscription, secret = webhook_service.rotate_secret(db, application, subscription_id)
        return {"webhook_secret": secret, "webhook_url": subscription.url, "webhook_rotated": True}

    return _action(request, session, db, slug, csrf, act, tab="webhooks", ok="")


@router.post("/applications/{slug}/webhooks/{subscription_id}/revoke")
def revoke_webhook(
    request: Request,
    slug: str,
    subscription_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    from app.services import webhooks as webhook_service

    def act(application):
        webhook_service.revoke_subscription(db, application, subscription_id)

    return _action(request, session, db, slug, csrf, act, tab="webhooks", ok="webhook_revoked")


@router.post("/applications/{slug}/webhooks/{subscription_id}/delete")
def delete_webhook(
    request: Request,
    slug: str,
    subscription_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    from app.services import webhooks as webhook_service

    def act(application):
        webhook_service.delete_subscription(db, application, subscription_id)

    return _action(request, session, db, slug, csrf, act, tab="webhooks", ok="webhook_deleted")


def _webhook_page(
    request, session, db, application, subscription_id, *, status=200, state="", **extra
):
    from app.services import webhooks as webhook_service

    try:
        subscription = webhook_service.get_subscription(db, application, subscription_id)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    state = state if state in ("pending", "delivered", "abandoned") else ""
    deliveries = webhook_service.list_deliveries(
        db, application, subscription_id, state=state or None, limit=100
    )
    return render(
        request,
        "webhook.html",
        session,
        status=status,
        subscription=subscription,
        deliveries=deliveries,
        state=state,
        **_app_context(db, application),
        **extra,
    )


@router.get("/applications/{slug}/webhooks/{subscription_id}")
def webhook(
    request: Request,
    slug: str,
    subscription_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    ok: str = "",
    state: str = "",
):
    application = _load_application(db, slug)
    return _webhook_page(
        request, session, db, application, subscription_id, state=state, notice=_notice(ok)
    )


@router.post("/applications/{slug}/webhooks/{subscription_id}/deliveries/{delivery_id}/replay")
def replay_delivery(
    request: Request,
    slug: str,
    subscription_id: uuid.UUID,
    delivery_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    from app.services import webhooks as webhook_service

    _csrf(request, session, csrf)
    application = _load_application(db, slug)
    try:
        webhook_service.replay_delivery(db, application, subscription_id, delivery_id)
        db.commit()
    except ServiceError as exc:
        db.rollback()
        return _webhook_page(
            request, session, db, application, subscription_id, status=400, error=exc.message
        )
    return redirect(f"/portal/applications/{slug}/webhooks/{subscription_id}?ok=replayed")


# --- domains -----------------------------------------------------------------------


@router.post("/applications/{slug}/domains")
def register_domain(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    hostname: str = Form(""),
    reference: str = Form(""),
):
    def act(application):
        domain = domain_service.claim_domain(db, application, hostname, reference)
        return f"/portal/applications/{slug}/domains/{domain.id}"

    return _action(request, session, db, slug, csrf, act, tab="domains", ok="domain_registered")


def _domain_page(request, session, db, application, domain_id, *, status=200, **extra):
    try:
        row = domain_service.get_domain(db, application, domain_id, include_deleted=True)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    events = domain_service.list_events(db, application, domain_id, limit=200)
    return render(
        request,
        "domain.html",
        session,
        status=status,
        domain=row,
        state=presenters.domain_state(row),
        records=presenters.record_views(row),
        checks=presenters.check_views(row),
        events=[(e, *presenters.describe_event(e)) for e in reversed(events)],
        **_app_context(db, application),
        **extra,
    )


@router.get("/applications/{slug}/domains/{domain_id}")
def domain(
    request: Request,
    slug: str,
    domain_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
    ok: str = "",
):
    application = _load_application(db, slug)
    return _domain_page(request, session, db, application, domain_id, notice=_notice(ok))


@router.post("/applications/{slug}/domains/{domain_id}/{verb}")
def domain_action(
    request: Request,
    slug: str,
    domain_id: uuid.UUID,
    verb: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
):
    actions = {
        "recheck": lambda a: domain_service.request_recheck(db, a, domain_id),
        "reissue": lambda a: domain_service.reissue_claim(db, a, domain_id),
        "delete": lambda a: domain_service.delete_domain(db, a, domain_id),
    }
    if verb not in actions:
        raise HTTPException(status_code=404)
    _csrf(request, session, csrf)
    application = _load_application(db, slug)
    try:
        actions[verb](application)
        db.commit()
    except ACTION_ERRORS as exc:
        db.rollback()
        message = getattr(exc, "message", None) or str(exc)
        return _domain_page(request, session, db, application, domain_id, status=400, error=message)
    if verb == "delete":
        return redirect(f"/portal/applications/{slug}/domains?ok=domain_delete")
    return redirect(f"/portal/applications/{slug}/domains/{domain_id}?ok=domain_{verb}")


# --- edge, doctor, legacy import -----------------------------------------------------


def _reconciler(request: Request):
    reconciler = getattr(request.app.state, "reconciler", None)
    if reconciler is not None:
        return reconciler
    from app.db.session import get_session_factory
    from app.edge.caddy_client import CaddyClient
    from app.edge.reconcile import Reconciler

    settings = request.app.state.edge_settings
    return Reconciler(get_session_factory(), CaddyClient(settings.admin_url), settings)


def _edge_page(request, session, db, *, verify=False, status=200, **extra):
    from sqlalchemy import select

    from app.edge.config import build_apps, hostnames_in, redact_apps_summary
    from app.models import EdgeLock
    from app.services.doctor import check_edge_name

    settings = request.app.state.edge_settings
    names = presenters.edge_names(db, settings)
    dns_settings = _dns_settings()
    resolve = _resolver(request)
    presenters.resolve_names(names, resolve, dns_settings)
    if verify:
        probe = _prober(request)
        for view in names:
            if view.addresses and not view.error:
                view.reachability = check_edge_name(
                    view.name, settings, dns_settings, resolve=resolve, probe_target=probe
                )
    apps = build_apps(db, settings)
    reconciler = getattr(request.app.state, "reconciler", None)
    return render(
        request,
        "edge.html",
        session,
        status=status,
        section="edge",
        settings=settings,
        names=names,
        verified=verify,
        hostnames=sorted(hostnames_in({"apps": apps})),
        summary=redact_apps_summary(apps),
        lock=db.scalar(select(EdgeLock).where(EdgeLock.name == "reconcile")),
        last_result=reconciler.last_result if reconciler else None,
        in_process=reconciler is not None,
        **extra,
    )


@router.get("/edge")
def edge(
    request: Request,
    session: dict = Operator,
    db: Session = DbSession,
    ok: str = "",
    verify: str = "",
):
    return _edge_page(request, session, db, verify=verify == "1", notice=_notice(ok))


@router.post("/edge/reconcile")
def reconcile(
    request: Request, session: dict = Operator, db: Session = DbSession, csrf: str = Form("")
):
    _csrf(request, session, csrf)
    result = _reconciler(request).run_once()
    if result.error:
        detail = f"{result.error}: {result.detail}" if result.detail else result.error
        return _edge_page(
            request,
            session,
            db,
            status=502,
            error=f"The edge refused or did not answer ({detail}).",
        )
    return redirect(
        "/portal/edge?ok=" + ("reconcile_applied" if result.changed else "reconcile_unchanged")
    )


@router.get("/doctor")
def doctor(request: Request, session: dict = Operator):
    from app.db.session import get_session_factory
    from app.services.doctor import run_doctor, summarize

    findings = run_doctor(
        get_session_factory(),
        request.app.state.edge_settings,
        _dns_settings(),
        resolve=_resolver(request),
        probe_target=_prober(request),
    )
    ok, warn, fail = summarize(findings)
    order = {"fail": 0, "warn": 1, "ok": 2}
    return render(
        request,
        "doctor.html",
        session,
        section="doctor",
        problems=sorted((f for f in findings if not f.ok), key=lambda f: order.get(f.status, 3)),
        passed=[f for f in findings if f.ok],
        ok=ok,
        warn=warn,
        fail=fail,
    )


@router.get("/legacy")
def legacy_form(request: Request, session: dict = Operator, db: Session = DbSession):
    return render(
        request,
        "legacy.html",
        session,
        section="legacy",
        applications=app_service.list_applications(db),
    )


@router.post("/legacy")
async def legacy_import(
    request: Request,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    application: str = Form(""),
    config: UploadFile = File(...),
    references: str = Form(""),
    port: int = Form(443),
    hostname_as_reference: bool = Form(False),
    grandfather: bool = Form(False),
    allow_skipped: bool = Form(False),
    dry_run: bool = Form(False),
):
    _csrf(request, session, csrf)
    applications = app_service.list_applications(db)
    form = {
        "application": application,
        "references": references,
        "port": port,
        "hostname_as_reference": hostname_as_reference,
        "grandfather": grandfather,
        "allow_skipped": allow_skipped,
    }

    def fail(message: str, status: int = 400):
        return render(
            request,
            "legacy.html",
            session,
            status=status,
            section="legacy",
            applications=applications,
            error=message,
            form=form,
        )

    try:
        entries = parse_legacy_config(json.loads(await config.read()), port=port)
        reference_map = json.loads(references) if references.strip() else {}
        if not isinstance(reference_map, dict):
            raise ValueError("the reference map must be a JSON object of hostname to reference")
    except (ValueError, TypeError) as exc:
        return fail(f"The input could not be read: {exc}")
    if not reference_map and not hostname_as_reference:
        return fail(
            "Give a reference map, or tick 'Use the hostname as the reference' so every "
            "hostname gets one."
        )
    try:
        target = app_service.get_application_by_slug(db, application)
        report = import_legacy_domains(
            db,
            target,
            entries,
            references=reference_map,
            grandfather=grandfather,
            hostname_as_reference=hostname_as_reference,
        )
    except ServiceError as exc:
        db.rollback()
        return fail(exc.message)
    committed = False
    if dry_run or (report.skipped and not allow_skipped):
        db.rollback()
    else:
        db.commit()
        committed = True
    return render(
        request,
        "legacy.html",
        session,
        section="legacy",
        applications=applications,
        report=report,
        committed=committed,
        dry_run=dry_run,
        blocked=bool(report.skipped and not allow_skipped and not dry_run),
        application_slug=application,
        form=form,
    )
