import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Depends
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.models import APIKey
from fastapi.openapi.utils import get_openapi
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import RedirectResponse, JSONResponse

from app.api import domain_api
from app.security import API_KEY_NAME, COOKIE_DOMAIN, get_api_key

# Load all environment variables from .env uploaded_file
load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI()
app.include_router(domain_api)

# CORS support
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', '*')
ALLOWED_METHODS = os.environ.get('ALLOWED_METHODS', '*')
ALLOWED_HEADERS = os.environ.get('ALLOWED_HEADERS', '*')

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=ALLOWED_METHODS,
    allow_headers=ALLOWED_HEADERS,
)

# Trusted Hosts
trusted_hosts = os.environ.get('TRUSTED_HOSTS', None)
if trusted_hosts:
    trusted_hosts = [host.strip() for host in trusted_hosts.split(",")]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


@app.get("/logout", tags=["default"])
async def logout_and_remove_cookie():
    response = RedirectResponse(url="/")
    response.delete_cookie(API_KEY_NAME, domain=COOKIE_DOMAIN)
    return response


@app.get("/openapi.json", tags=["default"])
async def get_open_api_endpoint(api_key: APIKey = Depends(get_api_key)):
    response = JSONResponse(
        get_openapi(title="SaaS HTTPS API", version='1.0.0', routes=app.routes)
    )
    return response


@app.get("/docs", tags=["default"])
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


@app.on_event("startup")
async def startup():
    logger.info("App started")


@app.on_event("shutdown")
async def shutdown():
    logger.info("App is shutting down")
