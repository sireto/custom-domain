"""Limits on live domains: per deployment (MAX_DOMAINS) and per application.

A live domain is one that is registered and not deleted, whatever its
status, including one still waiting for DNS. Tombstones do not count.
Limits only refuse *new* registrations; domains that already exist are never
affected, so lowering a limit below the current count stops growth without
switching anything off.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.edge.lock import DOMAIN_LIMIT_LOCK, acquire_lock
from app.models import Application, Domain
from app.services.errors import DomainLimitReached

# The doctor warns when usage reaches this share of a limit.
WARN_AT = 0.8


class InvalidLimit(ValueError):
    pass


def deployment_limit(environ: Mapping[str, str] | None = None) -> int | None:
    """``MAX_DOMAINS``: at most this many live domains across all applications."""
    env = os.environ if environ is None else environ
    raw = env.get("MAX_DOMAINS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise InvalidLimit(f"MAX_DOMAINS must be a whole number, not {raw!r}") from exc
    if value < 1:
        raise InvalidLimit("MAX_DOMAINS must be at least 1; leave it unset for no limit")
    return value


def validate_application_limit(value: int | None) -> int | None:
    if value is not None and value < 1:
        raise InvalidLimit("A domain limit must be at least 1, or none for no limit")
    return value


def live_domains(session: Session, application: Application | None = None) -> int:
    query = select(func.count()).select_from(Domain).where(Domain.deleted_at.is_(None))
    if application is not None:
        query = query.where(Domain.application_id == application.id)
    return session.scalar(query) or 0


@dataclass(frozen=True)
class Usage:
    scope: str  # "deployment" or "application"
    live: int
    limit: int

    @property
    def share(self) -> float:
        return self.live / self.limit

    @property
    def reached(self) -> bool:
        return self.live >= self.limit

    @property
    def near(self) -> bool:
        return self.share >= WARN_AT


def usages(session: Session, application: Application) -> list[Usage]:
    """The limits that apply to ``application``, with current counts, for display.

    An invalid ``MAX_DOMAINS`` is left out here; the doctor reports it.
    """
    found: list[Usage] = []
    if application.max_domains is not None:
        found.append(
            Usage("application", live_domains(session, application), application.max_domains)
        )
    try:
        limit = deployment_limit()
    except InvalidLimit:
        limit = None
    if limit is not None:
        found.append(Usage("deployment", live_domains(session), limit))
    return found


def enforce(session: Session, application: Application) -> None:
    """Refuse a new domain when a limit is reached.

    Call right after ``lock_application`` in ``domains.claim_domain``. The
    application lock makes the per-application count exact; with a
    deployment limit set, the ``domain-limit`` lock also serializes
    registrations across applications. The order (application, then
    deployment) is the same for every caller, so the two cannot deadlock.
    """
    limit = deployment_limit()
    if limit is not None:
        acquire_lock(session, DOMAIN_LIMIT_LOCK, f"application:{application.id}")
    if application.max_domains is not None:
        live = live_domains(session, application)
        if live >= application.max_domains:
            raise DomainLimitReached(
                f"Application '{application.slug}' has reached its limit of "
                f"{application.max_domains} live domains",
                details={"scope": "application", "limit": application.max_domains, "live": live},
            )
    if limit is not None:
        live = live_domains(session)
        if live >= limit:
            raise DomainLimitReached(
                f"This deployment has reached its limit of {limit} live domains",
                details={"scope": "deployment", "limit": limit, "live": live},
            )
