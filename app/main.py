import os

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

# Load all environment variables from .env uploaded_file
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from app.api import domain_api

# from api.contracts import contracts_api
# from api.document import doc_api
# from api.users import users_api
# from api.web import web_api
# from common.util import env_to_list

load_dotenv()

SERVER_HOST = os.environ.get('SERVER_HOST', 'localhost')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '50051'))

app = FastAPI()
app.include_router(domain_api)


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


# Open API /docs or /redoc page customization
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="SaaS HTTPS API",
        version="1.0.0",
        description="Open API spec of HTTPS API",
        routes=app.routes,
    )
    openapi_schema["info"]["x-logo"] = {
        "url": "https://s3.eu-central-1.wasabisys.com/eu.delta.sireto.io/assets/images/logo.png"
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.on_event("startup")
async def startup():
    print("app started")


@app.on_event("shutdown")
async def shutdown():
    print("app is shutting down")
