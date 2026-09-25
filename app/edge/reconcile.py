"""Converge the running Caddy configuration on the database.

The reconciler reads the authoritative state, builds the desired
configuration, and replaces Caddy's configuration only when it differs. It
never writes to the database, so a failed or rejected update cannot corrupt
domain state; Caddy keeps its last good configuration and the next run
retries. It runs at startup, on a timer, and after mutations that change the
route set.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.edge.caddy_client import CaddyClient, CaddyError, CaddyRejectedConfig, CaddyUnavailable
from app.edge.config import SERVER_NAME, build_apps, config_digest, hostnames_in
from app.edge.lock import acquire_reconcile_lock
from app.edge.settings import EdgeSettings
from app.models.types import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    at: datetime
    changed: bool
    desired_digest: str
    hostnames: int
    routes: int
    error: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Reconciler:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        client: CaddyClient,
        settings: EdgeSettings,
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.settings = settings
        self.last_result: ReconcileResult | None = None
        self.holder = f"{socket.gethostname()}:{os.getpid()}"
        # Serializes runs within this process; the database lock taken in
        # ``_run`` serializes runs across processes and instances.
        self._lock = threading.Lock()

    def desired_apps(self) -> dict[str, Any]:
        with self.session_factory() as session:
            return build_apps(session, self.settings)

    def run_once(self) -> ReconcileResult:
        """One convergence step. Never raises; failures are reported in the result."""
        with self._lock:
            result = self._run()
        self.last_result = result
        if result.ok:
            if result.changed:
                logger.info(
                    "edge config applied: %s routes, %s hostnames (%s)",
                    result.routes,
                    result.hostnames,
                    result.desired_digest,
                )
        else:
            logger.error("edge reconciliation failed (%s): %s", result.error, result.detail)
        return result

    def _run(self) -> ReconcileResult:
        now = utcnow()
        try:
            session = self.session_factory()
        except Exception as exc:  # database problems must not kill the loop
            return ReconcileResult(now, False, "none", 0, 0, "database_unavailable", str(exc))
        with session:
            try:
                # Hold the cross-instance lock from snapshot to Caddy write so
                # a snapshot built before a change committed cannot be applied
                # after a newer one (see app/edge/lock.py).
                acquire_reconcile_lock(session, self.holder)
                desired = build_apps(session, self.settings)
            except Exception as exc:
                session.rollback()
                return ReconcileResult(now, False, "none", 0, 0, "database_unavailable", str(exc))
            try:
                return self._apply(desired, now)
            finally:
                session.commit()  # releases the lock; nothing else to persist

    def _apply(self, desired: dict[str, Any], now: datetime) -> ReconcileResult:
        digest = config_digest(desired)
        hostnames = len(hostnames_in({"apps": desired}))
        routes = len(desired["http"]["servers"][SERVER_NAME]["routes"])

        try:
            current = self.client.get_config()
        except CaddyUnavailable as exc:
            return ReconcileResult(
                now, False, digest, hostnames, routes, "caddy_unavailable", str(exc)
            )
        if current is not None and current.get("apps") == desired:
            return ReconcileResult(now, False, digest, hostnames, routes)

        try:
            if current is None:
                # Caddy started without a bootstrap config (no storage block to
                # preserve); load the whole thing.
                self.client.load_config({"apps": desired})
            else:
                self.client.set_apps(desired)
        except CaddyRejectedConfig as exc:
            return ReconcileResult(
                now, False, digest, hostnames, routes, "config_rejected", str(exc)
            )
        except CaddyError as exc:
            return ReconcileResult(
                now, False, digest, hostnames, routes, "caddy_unavailable", str(exc)
            )
        return ReconcileResult(now, True, digest, hostnames, routes)

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        """Run immediately, then every ``interval`` seconds until ``stop`` is set."""
        while True:
            self.run_once()
            if stop.wait(interval):
                return
