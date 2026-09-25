"""Typed client for the v1 API.

Timeouts and retries: every request has a connect/read timeout (``timeout``,
10 s by default). Requests that are safe to repeat (GET, DELETE, and POST
create with an idempotency key) are retried up to ``max_retries`` times on
connection errors, timeouts, 429 (honouring Retry-After) and 5xx, with
exponential backoff. A create without an idempotency key and a recheck are
never retried automatically: the caller decides.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

import httpx

from custom_domain.errors import RateLimitedError, TransportError, error_for
from custom_domain.models import Delivery, Domain, Page, Webhook

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
USER_AGENT = "custom-domain-sdk/0.1"


class Client:
    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        if not credential.startswith("cd_"):
            raise ValueError("credential must be an application credential (cd_...)")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, max_retries)
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {credential}", "User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retryable: bool,
    ) -> httpx.Response:
        attempts = self.max_retries + 1 if retryable else 1
        delay = 0.5
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._http.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = TransportError(f"{type(exc).__name__}: {exc}")
                response = None
            if response is not None:
                if response.status_code < 400:
                    return response
                error = self._error(response)
                if not retryable or attempt == attempts - 1:
                    raise error
                if isinstance(error, RateLimitedError):
                    self._sleep(error.retry_after)
                    continue
                if response.status_code < 500:
                    raise error
                last_error = error
            if attempt < attempts - 1:
                self._sleep(delay)
                delay *= 2
        assert last_error is not None
        raise last_error

    @staticmethod
    def _error(response: httpx.Response):
        try:
            body = response.json().get("error", {})
        except ValueError:
            body = {}
        retry_after = response.headers.get("Retry-After")
        return error_for(
            response.status_code,
            body.get("code", "http_error"),
            body.get("message", response.text[:200]),
            body.get("details"),
            int(retry_after) if retry_after and retry_after.isdigit() else None,
        )

    # --- domains -----------------------------------------------------------

    def create_domain(
        self,
        hostname: str,
        reference: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Domain:
        """Register a hostname. Retried only when ``idempotency_key`` is given."""
        body: dict[str, Any] = {"hostname": hostname, "reference": reference}
        if metadata is not None:
            body["metadata"] = dict(metadata)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = self._request(
            "POST", "/v1/domains", json=body, headers=headers, retryable=idempotency_key is not None
        )
        return Domain.from_dict(response.json())

    def get_domain(self, domain_id: str | uuid.UUID, *, include_deleted: bool = False) -> Domain:
        params = {"include_deleted": "true"} if include_deleted else None
        response = self._request("GET", f"/v1/domains/{domain_id}", params=params, retryable=True)
        return Domain.from_dict(response.json())

    def list_domains(
        self,
        *,
        reference: str | None = None,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if reference is not None:
            params["reference"] = reference
        if status is not None:
            params["status"] = status
        if include_deleted:
            params["include_deleted"] = "true"
        data = self._request("GET", "/v1/domains", params=params, retryable=True).json()
        return Page(
            items=[Domain.from_dict(d) for d in data["items"]],
            limit=data["limit"],
            offset=data["offset"],
            next_offset=data.get("next_offset"),
        )

    def iter_domains(
        self, *, reference: str | None = None, status: str | None = None, page_size: int = 100
    ) -> Iterator[Domain]:
        offset = 0
        while True:
            page = self.list_domains(
                reference=reference, status=status, limit=page_size, offset=offset
            )
            yield from page.items
            if page.next_offset is None:
                return
            offset = page.next_offset

    def request_recheck(self, domain_id: str | uuid.UUID) -> Domain:
        """Ask for all checks to run again. Not retried: rate limited server side."""
        response = self._request("POST", f"/v1/domains/{domain_id}/checks", retryable=False)
        return Domain.from_dict(response.json())

    def delete_domain(self, domain_id: str | uuid.UUID) -> Domain:
        response = self._request("DELETE", f"/v1/domains/{domain_id}", retryable=True)
        return Domain.from_dict(response.json())

    # --- webhooks ----------------------------------------------------------

    def create_webhook(self, url: str, events: list[str]) -> Webhook:
        response = self._request(
            "POST", "/v1/webhooks", json={"url": url, "events": events}, retryable=False
        )
        return Webhook.from_dict(response.json())

    def list_webhooks(self) -> list[Webhook]:
        return [
            Webhook.from_dict(w)
            for w in self._request("GET", "/v1/webhooks", retryable=True).json()
        ]

    def revoke_webhook(self, webhook_id: str | uuid.UUID) -> Webhook:
        return Webhook.from_dict(
            self._request("DELETE", f"/v1/webhooks/{webhook_id}", retryable=True).json()
        )

    def rotate_webhook_secret(self, webhook_id: str | uuid.UUID) -> Webhook:
        return Webhook.from_dict(
            self._request("POST", f"/v1/webhooks/{webhook_id}/rotate", retryable=False).json()
        )

    def list_deliveries(
        self,
        webhook_id: str | uuid.UUID,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Delivery]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if state:
            params["state"] = state
        rows = self._request(
            "GET", f"/v1/webhooks/{webhook_id}/deliveries", params=params, retryable=True
        ).json()
        return [Delivery.from_dict(d) for d in rows]

    def replay_delivery(
        self, webhook_id: str | uuid.UUID, delivery_id: str | uuid.UUID
    ) -> Delivery:
        return Delivery.from_dict(
            self._request(
                "POST", f"/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/replay", retryable=True
            ).json()
        )

    def replay_since(self, webhook_id: str | uuid.UUID, since: datetime) -> int:
        data = self._request(
            "POST",
            f"/v1/webhooks/{webhook_id}/replay",
            params={"since": since.isoformat()},
            retryable=True,
        ).json()
        return int(data["requeued"])
