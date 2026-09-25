"""Minimal client for the Caddy admin API.

Only two operations are needed: read the running configuration and replace
it atomically. Caddy validates a new configuration before applying it and
keeps the previous one when it is rejected, which is what makes the
reconciler safe to retry.
"""

from __future__ import annotations

from typing import Any

import httpx


class CaddyError(Exception):
    pass


class CaddyUnavailable(CaddyError):
    """The admin API could not be reached."""


class CaddyRejectedConfig(CaddyError):
    """Caddy validated the configuration and refused it; the old config still runs."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Caddy rejected the configuration ({status_code}): {body[:500]}")
        self.status_code = status_code
        self.body = body


class CaddyClient:
    def __init__(self, admin_url: str, *, timeout: float = 10.0) -> None:
        self.admin_url = admin_url.rstrip("/")
        self._client = httpx.Client(base_url=self.admin_url, timeout=timeout)

    def get_config(self) -> dict[str, Any] | None:
        try:
            response = self._client.get("/config/")
        except httpx.HTTPError as exc:
            raise CaddyUnavailable(
                f"Cannot reach Caddy admin API at {self.admin_url}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise CaddyUnavailable(
                f"Caddy admin API returned {response.status_code} for GET /config/"
            )
        if not response.content or response.content.strip() == b"null":
            return None
        return response.json()

    def load_config(self, config: dict[str, Any]) -> None:
        try:
            response = self._client.post("/load", json=config)
        except httpx.HTTPError as exc:
            raise CaddyUnavailable(
                f"Cannot reach Caddy admin API at {self.admin_url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise CaddyRejectedConfig(response.status_code, response.text)

    def set_apps(self, apps: dict[str, Any]) -> None:
        """Replace only the ``apps`` subtree; ``admin`` and ``storage`` stay as loaded."""
        try:
            response = self._client.post("/config/apps", json=apps)
        except httpx.HTTPError as exc:
            raise CaddyUnavailable(
                f"Cannot reach Caddy admin API at {self.admin_url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise CaddyRejectedConfig(response.status_code, response.text)

    def close(self) -> None:
        self._client.close()
