"""ASGI application factory.

The v1 API (``/v1``) is the supported contract. The legacy single-application
endpoints under ``/domains`` stay available while ``ENABLE_LEGACY_API`` is
true (the default) so existing deployments can migrate; see docs/api-v1.md.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.models import APIKey
from fastapi.openapi.utils import get_openapi
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from app.db.session import get_session_factory
from app.dns.resolver import SystemResolver
from app.dns.settings import DnsSettings
from app.dns.worker import ChecksWorker
from app.edge.caddy_client import CaddyClient
from app.edge.reconcile import Reconciler
from app.edge.settings import EdgeSettings
from app.services.edge_checks import SystemEdgeProber
from app.v1.errors import install_error_handlers
from app.v1.internal import router as internal_router
from app.v1.router import router as v1_router
from app.v1.webhooks import webhooks
from app.v1.webhooks_api import router as webhooks_router
from app.webhooks.worker import WebhookWorker

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Custom domains for multi-tenant SaaS. Applications register customer hostnames
for their workspaces, hand the returned DNS records to the customer, and are
told when the hostname is verified, certified and serving.

Applications are registered by the operator (`custom-domain application create`)
and receive credentials from the operator (`custom-domain credential issue`).
There is no self-service registration endpoint in v1. Every request is scoped
to the application that owns the presented credential.

The endpoints under `/domains` without a version prefix are the legacy
single-application API. They are deprecated and disabled by setting
`ENABLE_LEGACY_API=false`.
"""


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = EdgeSettings.from_env()
    app.state.edge_settings = settings
    stop = threading.Event()
    thread: threading.Thread | None = None
    app.state.reconciler = None
    if settings.reconcile_enabled:
        reconciler = Reconciler(get_session_factory(), CaddyClient(settings.admin_url), settings)
        app.state.reconciler = reconciler
        thread = threading.Thread(
            target=reconciler.run_forever,
            args=(stop, settings.reconcile_interval),
            name="edge-reconciler",
            daemon=True,
        )
        thread.start()
        logger.info("edge reconciler started (every %ss)", settings.reconcile_interval)
    else:
        logger.info("edge reconciler disabled; the legacy API owns the Caddy config")

    dns_settings = DnsSettings.from_env()
    dns_thread: threading.Thread | None = None
    app.state.dns_worker = None
    if dns_settings.worker_enabled:
        worker = ChecksWorker(
            get_session_factory(),
            SystemResolver(dns_settings.nameservers or None, timeout=dns_settings.timeout),
            prober=SystemEdgeProber(settings),
            settings=settings,
            batch_size=dns_settings.batch_size,
            on_status_change=(
                app.state.reconciler.run_once if app.state.reconciler is not None else None
            ),
        )
        app.state.dns_worker = worker
        dns_thread = threading.Thread(
            target=worker.run_forever,
            args=(stop, dns_settings.worker_interval),
            name="dns-worker",
            daemon=True,
        )
        dns_thread.start()
        logger.info("dns worker started (every %ss)", dns_settings.worker_interval)
    webhook_thread: threading.Thread | None = None
    app.state.webhook_worker = None
    if _env_flag("WEBHOOK_WORKER_ENABLED", True):
        webhook_worker = WebhookWorker(get_session_factory())
        app.state.webhook_worker = webhook_worker
        webhook_thread = threading.Thread(
            target=webhook_worker.run_forever,
            args=(stop, float(os.environ.get("WEBHOOK_WORKER_INTERVAL", "5"))),
            name="webhook-worker",
            daemon=True,
        )
        webhook_thread.start()
    logger.info("App started")
    yield
    stop.set()
    for worker_thread in (thread, dns_thread, webhook_thread):
        if worker_thread is not None:
            worker_thread.join(timeout=5)
    logger.info("App is shutting down")


def create_app() -> FastAPI:
    load_dotenv()
    from app.observability import install_log_redaction

    install_log_redaction()
    app = FastAPI(
        title="Custom Domain API",
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(v1_router)
    app.include_router(internal_router)
    app.include_router(webhooks_router)
    app.webhooks.include_router(webhooks)

    if _env_flag("ENABLE_LEGACY_API", True):
        _mount_legacy(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_csv("ALLOWED_ORIGINS", "*"),
        allow_credentials=True,
        allow_methods=_csv("ALLOWED_METHODS", "*"),
        allow_headers=_csv("ALLOWED_HEADERS", "*"),
    )
    trusted_hosts = _csv("TRUSTED_HOSTS", "")
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    return app


def _mount_legacy(app: FastAPI) -> None:
    from app.api import domain_api
    from app.security import API_KEY_NAME, COOKIE_DOMAIN, get_api_key

    app.include_router(domain_api)

    @app.get("/logout", tags=["legacy"], deprecated=True, include_in_schema=False)
    async def logout_and_remove_cookie():
        response = RedirectResponse(url="/")
        response.delete_cookie(API_KEY_NAME, domain=COOKIE_DOMAIN)
        return response

    @app.get("/openapi.json", tags=["legacy"], deprecated=True, include_in_schema=False)
    async def get_open_api_endpoint(api_key: APIKey = Depends(get_api_key)):
        return JSONResponse(get_openapi(title="SaaS HTTPS API", version="1.0.0", routes=app.routes))

    @app.get("/docs", tags=["legacy"], deprecated=True, include_in_schema=False)
    async def get_documentation(api_key: APIKey = Depends(get_api_key)):
        response = get_swagger_ui_html(openapi_url="/openapi.json", title="docs")
        response.set_cookie(
            API_KEY_NAME,
            value=api_key,
            domain=COOKIE_DOMAIN,
            httponly=True,
            max_age=1800,
            expires=1800,
        )
        return response


app = create_app()
