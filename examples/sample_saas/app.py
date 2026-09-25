"""Minimal SaaS origin using the SDK middleware.

Run:
    EDGE_ASSERTION_KEYS="1:<secret>" APPLICATION_ID=<uuid> \
    uvicorn examples.sample_saas.app:app --port 8000

Each workspace is a row in WORKSPACES; the page shows which one the edge
routed the request for. Invented data only.
"""

from __future__ import annotations

import os

from custom_domain import CustomDomainMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

WORKSPACES = {
    "ws_alpha": {"name": "Alpha Forms", "colour": "#2b6cb0"},
    "ws_beta": {"name": "Beta Surveys", "colour": "#2f855a"},
}


def load_keys() -> dict[str, str]:
    keys = {}
    for item in os.environ.get("EDGE_ASSERTION_KEYS", "").split(","):
        key_id, sep, secret = item.strip().partition(":")
        if sep:
            keys[key_id] = secret
    return keys


def create_app(keys: dict[str, str] | None = None, application_id: str | None = None) -> FastAPI:
    app = FastAPI(title="Sample SaaS origin")
    app.add_middleware(
        CustomDomainMiddleware,
        keys=keys if keys is not None else load_keys(),
        application_id=application_id or os.environ.get("APPLICATION_ID", ""),
        workspace_lookup=WORKSPACES.get,  # the probe answers only for workspaces that exist
        on_missing="reject",
    )

    @app.get("/.well-known/custom-domain-origin-verification", response_class=PlainTextResponse)
    def origin_verification() -> str:
        # Plain text, exactly the token: the verifier compares the body byte for byte.
        return os.environ.get("ORIGIN_VERIFICATION_TOKEN", "")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        assertion = request.state.custom_domain
        workspace = WORKSPACES.get(assertion.reference)
        if workspace is None:
            return HTMLResponse("<h1>Unknown workspace</h1>", status_code=404)
        return (
            f"<h1 style='color:{workspace['colour']}'>{workspace['name']}</h1>"
            f"<p>served for {assertion.hostname} (workspace {assertion.reference})</p>"
        )

    return app


app = create_app()
