"""Portal pages: the operator actions of the ``custom-domain`` command in a browser.

Every action calls the same service function the command line does, so the
two stay equivalent. Secrets (credentials, verification tokens) are rendered
once, in the response to the action that produced them, and never stored in
the session.
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
from starlette.responses import RedirectResponse, Response

from app.db.session import get_session
from app.hostname import InvalidHostname
from app.legacy import import_legacy_domains, parse_legacy_config
from app.models import ApplicationStatus, DomainStatus
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


# --- dashboard -------------------------------------------------------------------


@router.get("")
@router.get("/")
def dashboard(request: Request, session: dict = Operator, db: Session = DbSession, ok: str = ""):
    from sqlalchemy import func, select

    from app.models import Application, Domain, EdgeLock

    applications = app_service.list_applications(db)
    counts = dict(
        db.execute(
            select(Domain.status, func.count())
            .where(Domain.deleted_at.is_(None))
            .group_by(Domain.status)
        ).all()
    )
    lock = db.scalar(select(EdgeLock).where(EdgeLock.name == "reconcile"))
    edge_settings = getattr(request.app.state, "edge_settings", None)
    reconciler = getattr(request.app.state, "reconciler", None)
    return render(
        request,
        "dashboard.html",
        session,
        applications=applications,
        application_count=db.scalar(select(func.count()).select_from(Application)),
        counts={status.value: counts.get(status, 0) for status in DomainStatus},
        lock=lock,
        edge_settings=edge_settings,
        last_reconcile=reconciler.last_result if reconciler else None,
        notice=ok,
    )


@router.post("/maintenance/purge-tombstones")
def purge_tombstones(
    request: Request, session: dict = Operator, db: Session = DbSession, csrf: str = Form("")
):
    _csrf(request, session, csrf)
    count = domain_service.purge_tombstones(db)
    db.commit()
    return redirect(f"/portal?ok=purged+{count}+tombstone(s)")


# --- applications ------------------------------------------------------------------


@router.get("/applications")
def applications(request: Request, session: dict = Operator, db: Session = DbSession, ok: str = ""):
    return render(
        request,
        "applications.html",
        session,
        applications=app_service.list_applications(db),
        notice=ok,
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
        return render(
            request,
            "applications.html",
            session,
            status=400,
            applications=app_service.list_applications(db),
            error=str(exc),
            form={"slug": slug, "name": name, "cname_target": cname_target},
        )
    return redirect(f"/portal/applications/{application.slug}?ok=application+created")


def _load_application(db: Session, slug: str):
    try:
        return app_service.get_application_by_slug(db, slug)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _application_page(
    request: Request,
    session: dict,
    db: Session,
    application,
    *,
    status: int = 200,
    domain_status: str | None = None,
    offset: int = 0,
    **extra: Any,
):
    wanted = None
    if domain_status:
        try:
            wanted = DomainStatus(domain_status)
        except ValueError:
            wanted = None
    domains, has_more = domain_service.page_domains(
        db,
        application,
        status=wanted,
        include_deleted=domain_status == "deleting",
        limit=50,
        offset=max(0, offset),
    )
    return render(
        request,
        "application.html",
        session,
        status=status,
        application=application,
        origins=app_service.list_origins(db, application),
        credentials=app_service.list_credentials(db, application),
        edge_names=app_service.edge_names(db, application),
        domains=domains,
        has_more=has_more,
        offset=max(0, offset),
        domain_status=domain_status or "",
        statuses=[s.value for s in DomainStatus],
        **extra,
    )


@router.get("/applications/{slug}")
def application(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    ok: str = "",
    status: str = "",
    offset: int = 0,
):
    application = _load_application(db, slug)
    return _application_page(
        request, session, db, application, domain_status=status, offset=offset, notice=ok
    )


def _action(request, session, db, slug, csrf, fn, ok):
    """Run a service action for an application; render the page with the error on failure."""
    _csrf(request, session, csrf)
    application = _load_application(db, slug)
    try:
        result = fn(application)
        db.commit()
    except (ServiceError, OriginVerificationFailed, InvalidHostname, ValueError) as exc:
        db.rollback()
        message = getattr(exc, "message", None) or str(exc)
        return _application_page(request, session, db, application, status=400, error=message)
    if isinstance(result, Response):
        return result
    if result is not None:
        # An action that produced a secret: show it once on the page.
        return _application_page(request, session, db, application, **result)
    return redirect(f"/portal/applications/{slug}?ok={ok}")


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

    return _action(request, session, db, slug, csrf, act, "cname+target+updated")


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

    return _action(request, session, db, slug, csrf, act, "workspace+probe+updated")


@router.post("/applications/{slug}/status")
def set_status(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    status: str = Form(""),
):
    def act(application):
        app_service.set_application_status(db, application, ApplicationStatus(status))

    return _action(request, session, db, slug, csrf, act, "status+updated")


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
        origin = app_service.register_origin(db, application, host=host, scheme=scheme, port=port)
        return {"origin_token": origin.verification_token, "origin_url": origin.url}

    return _action(request, session, db, slug, csrf, act, "origin+registered")


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
        origin = app_service.get_origin(db, application, origin_id=origin_id)
        try:
            verify_origin(db, origin, allow_private=allow_private_from_env())
        except OriginVerificationFailed:
            db.commit()  # the failure is recorded on the origin
            raise
        if activate:
            app_service.activate_origin(db, origin)

    return _action(request, session, db, slug, csrf, act, "origin+verified")


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
        origin = app_service.get_origin(db, application, origin_id=origin_id)
        app_service.activate_origin(db, origin)

    return _action(request, session, db, slug, csrf, act, "origin+activated")


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
        app_service.retire_origin(db, app_service.get_origin(db, application, origin_id=origin_id))

    return _action(request, session, db, slug, csrf, act, "origin+retired")


# --- credentials --------------------------------------------------------------------


@router.post("/applications/{slug}/credentials")
def issue_credential(
    request: Request,
    slug: str,
    session: dict = Operator,
    db: Session = DbSession,
    csrf: str = Form(""),
    label: str = Form(""),
    expires_in_days: int | None = Form(None),
):
    def act(application):
        from app.models.types import utcnow

        expires_at = utcnow() + timedelta(days=expires_in_days) if expires_in_days else None
        credential, secret = app_service.issue_credential(
            db, application, label=label, expires_at=expires_at
        )
        return {"credential_secret": secret, "credential_label": credential.label}

    return _action(request, session, db, slug, csrf, act, "credential+issued")


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

    return _action(request, session, db, slug, csrf, act, "credential+revoked")


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
        credential, secret, _old = app_service.rotate_credential(
            db, application, credential_id, grace=timedelta(hours=max(0, grace_hours))
        )
        return {"credential_secret": secret, "credential_label": credential.label}

    return _action(request, session, db, slug, csrf, act, "credential+rotated")


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
        domain_service.claim_domain(db, application, hostname, reference)

    return _action(request, session, db, slug, csrf, act, "domain+registered")


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

    def act(application):
        actions[verb](application)

    return _action(request, session, db, slug, csrf, act, f"domain+{verb}+done")


@router.get("/applications/{slug}/domains/{domain_id}")
def domain(
    request: Request,
    slug: str,
    domain_id: uuid.UUID,
    session: dict = Operator,
    db: Session = DbSession,
):
    application = _load_application(db, slug)
    try:
        row = domain_service.get_domain(db, application, domain_id, include_deleted=True)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    from app.v1.schemas import dns_records_for

    return render(
        request,
        "domain.html",
        session,
        application=application,
        domain=row,
        records=dns_records_for(row),
        events=domain_service.list_events(db, application, domain_id, limit=200),
    )


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


@router.get("/edge")
def edge(request: Request, session: dict = Operator, db: Session = DbSession, ok: str = ""):
    from app.edge.config import build_apps, hostnames_in, redact_apps_summary

    settings = request.app.state.edge_settings
    apps = build_apps(db, settings)
    reconciler = getattr(request.app.state, "reconciler", None)
    return render(
        request,
        "edge.html",
        session,
        settings=settings,
        hostnames=sorted(hostnames_in({"apps": apps})),
        summary=redact_apps_summary(apps),
        last_result=reconciler.last_result if reconciler else None,
        in_process=reconciler is not None,
        notice=ok,
    )


@router.post("/edge/reconcile")
def reconcile(request: Request, session: dict = Operator, csrf: str = Form("")):
    _csrf(request, session, csrf)
    result = _reconciler(request).run_once()
    outcome = "applied" if result.changed else (result.error or "unchanged")
    detail = f"+{result.detail}" if result.detail else ""
    return redirect(f"/portal/edge?ok=reconcile:+{outcome}{detail}"[:500])


@router.get("/doctor")
def doctor(request: Request, session: dict = Operator):
    from app.db.session import get_session_factory
    from app.dns.settings import DnsSettings
    from app.services.doctor import run_doctor, summarize

    findings = run_doctor(
        get_session_factory(), request.app.state.edge_settings, DnsSettings.from_env()
    )
    ok, warn, fail = summarize(findings)
    return render(request, "doctor.html", session, findings=findings, ok=ok, warn=warn, fail=fail)


@router.get("/legacy")
def legacy_form(request: Request, session: dict = Operator, db: Session = DbSession):
    return render(request, "legacy.html", session, applications=app_service.list_applications(db))


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

    def fail(message: str, status: int = 400):
        return render(
            request, "legacy.html", session, status=status, applications=applications, error=message
        )

    try:
        entries = parse_legacy_config(json.loads(await config.read()), port=port)
        reference_map = json.loads(references) if references.strip() else {}
        if not isinstance(reference_map, dict):
            raise ValueError("the reference map must be a JSON object of hostname to reference")
    except (ValueError, TypeError) as exc:
        return fail(f"cannot read the input: {exc}")
    if not reference_map and not hostname_as_reference:
        return fail("give a reference map, or tick 'use the hostname as the reference'")
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
        return fail(str(exc))
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
        applications=applications,
        report=report,
        committed=committed,
        dry_run=dry_run,
        blocked=bool(report.skipped and not allow_skipped and not dry_run),
        application_slug=application,
    )
