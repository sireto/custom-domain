"""ASGI application factory.

The v1 API (``/v1``) is the supported contract. The legacy single-application
endpoints under ``/domains`` stay available while ``ENABLE_LEGACY_API`` is
true (the default) so existing deployments can migrate; see docs/api-v1.md.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.models import APIKey
from fastapi.openapi.utils import get_openapi
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from app.v1.errors import install_error_handlers
from app.v1.router import router as v1_router
from app.v1.webhooks import webhooks

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
    logger.info("App started")
    yield
    logger.info("App is shutting down")


def create_app() -> FastAPI:
    load_dotenv()
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
