"""Per-application traffic, counted at the edge.

Caddy counts each hostname's requests and response bytes
(``apps.http.metrics.per_host``) and the reconciler reads those totals on
every run (``app/edge/reconcile.py``). Caddy's counters start again at zero
whenever its configuration changes or it restarts, so each reading is turned
into an increase against the totals stored for that hostname at the previous
reading, and the increase is added to the day's row of the application that
owns the hostname now.

The reconciler reads the totals just before it applies a new configuration
and calls ``reset_baselines`` once the new one is in place, so a
configuration change loses only the requests in between. A restart of Caddy
is noticed when a total goes down; the requests since the previous reading,
up to the restart, are not counted.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import Application, ApplicationTraffic, Domain, EdgeTrafficCounter
from app.models.types import utcnow
from app.services.errors import InvalidApplication

TRAFFIC_RETENTION_DAYS = 400
# Caddy's label for requests to a hostname it is not configured for.
OTHER_HOST = "_other"
COUNTER_EXPIRY = timedelta(days=30)


@dataclass(frozen=True)
class TrafficDay:
    day: date
    requests: int
    response_bytes: int


def record_totals(
    session: Session,
    totals: Mapping[str, tuple[int, int]],
    *,
    now: datetime | None = None,
) -> int:
    """Add the increase since the previous reading to each owner's day.

    ``totals`` maps a hostname to the edge's running (requests, response
    bytes). Returns the number of applications whose day changed.
    """
    now = now or utcnow()
    readings = {
        host.lower(): (max(int(requests), 0), max(int(sent), 0))
        for host, (requests, sent) in totals.items()
        if host != OTHER_HOST
    }
    counters = {c.hostname: c for c in session.scalars(select(EdgeTrafficCounter))}
    owners: dict[str, uuid.UUID] = dict(
        session.execute(
            select(Domain.hostname, Domain.application_id).where(Domain.deleted_at.is_(None))
        ).all()
    )
    increases: dict[uuid.UUID, list[int]] = {}
    for host, (requests, sent) in readings.items():
        counter = counters.get(host)
        if counter is None:
            counter = EdgeTrafficCounter(hostname=host, requests=0, response_bytes=0)
            session.add(counter)
        if requests < counter.requests or sent < counter.response_bytes:
            # Caddy restarted since the previous reading: count from zero.
            added = (requests, sent)
        else:
            added = (requests - counter.requests, sent - counter.response_bytes)
        counter.requests, counter.response_bytes, counter.read_at = requests, sent, now
        owner = owners.get(host)
        if owner is not None and (added[0] or added[1]):
            total = increases.setdefault(owner, [0, 0])
            total[0] += added[0]
            total[1] += added[1]
    today = now.date()
    for application_id, (requests, sent) in increases.items():
        row = session.get(ApplicationTraffic, (application_id, today))
        if row is None:
            session.add(
                ApplicationTraffic(
                    application_id=application_id,
                    day=today,
                    requests=requests,
                    response_bytes=sent,
                )
            )
        else:
            row.requests += requests
            row.response_bytes += sent
    # Hostnames the edge stopped reporting (removed, or Caddy restarted
    # without them) need no baseline after a while.
    session.execute(
        delete(EdgeTrafficCounter).where(EdgeTrafficCounter.read_at < now - COUNTER_EXPIRY)
    )
    session.execute(
        delete(ApplicationTraffic).where(
            ApplicationTraffic.day <= today - timedelta(days=TRAFFIC_RETENTION_DAYS)
        )
    )
    session.flush()
    return len(increases)


def reset_baselines(session: Session) -> None:
    """The edge's counters were just restarted by a new configuration."""
    session.execute(update(EdgeTrafficCounter).values(requests=0, response_bytes=0))
    session.flush()


def application_traffic(
    session: Session,
    application: Application,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> list[TrafficDay]:
    """The application's last ``days`` UTC days, oldest first, today included.

    Days without traffic are listed with zeros.
    """
    if not 1 <= days <= TRAFFIC_RETENTION_DAYS:
        raise InvalidApplication(f"Days must be 1 to {TRAFFIC_RETENTION_DAYS}")
    today = (now or utcnow()).date()
    first = today - timedelta(days=days - 1)
    rows = {
        row.day: row
        for row in session.scalars(
            select(ApplicationTraffic).where(
                ApplicationTraffic.application_id == application.id,
                ApplicationTraffic.day >= first,
            )
        )
    }
    result = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        row = rows.get(day)
        result.append(TrafficDay(day, row.requests if row else 0, row.response_bytes if row else 0))
    return result


def human_bytes(count: int) -> str:
    """1500 -> "1.5 KB", in decimal units."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")
