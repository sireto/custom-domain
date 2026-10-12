"""Which DNS provider hosts each domain's zone, and how its records get set up.

The DNS worker calls ``ZoneDetector.refresh`` for each domain it checks. It
finds the zone (app/dns/zones.py), identifies the provider from the zone's
nameservers (app/dns/providers.py), runs Domain Connect discovery
(app/dns/domain_connect.py) and caches the result per zone in ``dns_zones``
for ``ZONE_TTL`` (``RETRY_TTL`` after a transient failure). Detection is
best effort: a failure is recorded on the zone and never affects the
domain's checks or status.

``domain_setup`` turns that into the ``setup`` object of the v1 API:

* ``domain_connect``: the provider supports the configured Domain Connect
  template and the service can sign apply requests;
* ``guided``: a provider-specific setup guide exists;
* ``manual``: any other provider (the generic guide);
* ``None`` until the zone has been looked at.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dns import providers
from app.dns.domain_connect import Discovery, Fetch, discover, guarded_fetch
from app.dns.resolver import Resolver
from app.dns.settings import SetupSettings
from app.dns.zones import ZoneLookup, default_zone, find_zone, host_in_zone
from app.models import Application, DnsZone, Domain, DomainStatus
from app.models.types import utcnow

logger = logging.getLogger(__name__)

ZONE_TTL = timedelta(hours=24)
RETRY_TTL = timedelta(hours=1)
# Expired zone rows no live or tombstoned domain refers to are purged after this.
ZONE_RETENTION = timedelta(days=7)

DOMAIN_CONNECT = "domain_connect"
GUIDED = "guided"
MANUAL = "manual"
METHODS = (DOMAIN_CONNECT, GUIDED, MANUAL)


@lru_cache(maxsize=1)
def setup_settings() -> SetupSettings:
    """The deployment's settings, read from the environment once."""
    return SetupSettings.from_env()


# --- method selection ------------------------------------------------------------


def select_method(
    provider_id: str | None,
    template_supported: bool | None,
    template: str | None,
    settings: SetupSettings,
) -> str:
    """The setup method for a zone the worker has looked at."""
    if (
        template_supported
        and settings.signing_enabled
        and settings.template_key is not None
        and template == settings.template_key
    ):
        return DOMAIN_CONNECT
    return GUIDED if providers.get_provider(provider_id).guide else MANUAL


def zone_provider(zone: DnsZone) -> tuple[str, str]:
    """``(id, name)`` of the zone's provider, for display.

    An unidentified provider that answered Domain Connect discovery is shown
    under the name it gave itself (cleaned and length limited on the way in).
    """
    provider = providers.get_provider(zone.provider)
    if provider.id == providers.UNKNOWN and zone.domain_connect:
        name = zone.domain_connect.get("provider_display_name") or zone.domain_connect.get(
            "provider_name"
        )
        if name:
            return provider.id, str(name)
    return provider.id, provider.name


@dataclass(frozen=True)
class Setup:
    zone: str
    host: str
    method: str | None
    provider_id: str | None
    provider_name: str | None
    guide_url: str | None
    detected_at: datetime | None


def domain_setup(domain: Domain, settings: SetupSettings | None = None) -> Setup:
    if settings is None:
        from app.services.domain_connect import settings_for

        settings = settings_for(domain)
    zone_name = domain.zone or default_zone(domain.hostname)
    try:
        host = host_in_zone(domain.hostname, zone_name)
    except ValueError:  # a stale zone that no longer encloses the name
        zone_name = default_zone(domain.hostname)
        host = host_in_zone(domain.hostname, zone_name)
    zone = domain.dns_zone if domain.zone else None
    if zone is None:
        return Setup(zone_name, host, None, None, None, None, None)
    method = select_method(zone.provider, zone.template_supported, zone.template, settings)
    provider_id, provider_name = zone_provider(zone)
    return Setup(
        zone=zone_name,
        host=host,
        method=method,
        provider_id=provider_id,
        provider_name=provider_name,
        guide_url=settings.guide_for(providers.get_provider(provider_id).guide_slug),
        detected_at=zone.checked_at,
    )


# --- detection (DNS worker) ------------------------------------------------------


def _fresh(zone: DnsZone | None, now: datetime, template: str | None) -> bool:
    return zone is not None and zone.expires_at > now and zone.template == template


def identify(lookup: ZoneLookup, discovery: Discovery) -> providers.Provider:
    """The provider from the nameservers, else from Domain Connect's ``providerId``."""
    provider = providers.provider_for_nameservers(lookup.nameservers)
    if provider.id == providers.UNKNOWN and discovery.settings:
        named = providers.provider_for_domain_connect(discovery.settings.get("provider_id"))
        if named is not None:
            return named
    return provider


class ZoneDetector:
    """Finds and caches each domain's zone, provider and Domain Connect support."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        resolver: Resolver,
        *,
        settings: SetupSettings | None = None,
        fetch: Fetch = guarded_fetch,
    ) -> None:
        self.session_factory = session_factory
        self.resolver = resolver
        # Given settings are used as they are (tests); otherwise each refresh
        # reads the effective ones, so a signer or template set through the
        # operator API applies without a restart.
        self._settings = settings
        self.fetch = fetch

    @property
    def settings(self) -> SetupSettings:
        if self._settings is not None:
            return self._settings
        from app.services.domain_connect import effective_setup_settings

        with self.session_factory() as session:
            return effective_setup_settings(session)

    def refresh(
        self, hostname: str, known_zone: str | None, *, now: datetime | None = None
    ) -> str | None:
        """The hostname's zone, refreshed in the cache if it is stale.

        Runs outside any domain transaction (it may make network requests).
        Returns None if detection itself broke; that is logged, never raised.
        """
        now = now or utcnow()
        try:
            settings = self.settings
            template = settings.template_key
            if known_zone is not None and self._is_fresh(known_zone, now, template):
                return known_zone
            lookup = find_zone(self.resolver, hostname)
            if lookup.zone != known_zone and self._is_fresh(lookup.zone, now, template):
                return lookup.zone
            discovery = discover(
                self.resolver,
                lookup.zone,
                provider_id=settings.template_provider_id or None,
                service_id=settings.template_service_id,
                fetch=self.fetch,
            )
            self._store(lookup, discovery, template, now)
            return lookup.zone
        except Exception:  # detection must never break the checks
            logger.exception("dns setup detection failed for %s", hostname)
            return None

    def _is_fresh(self, zone: str, now: datetime, template: str | None) -> bool:
        with self.session_factory() as session:
            row = session.scalar(select(DnsZone).where(DnsZone.zone == zone))
            return _fresh(row, now, template)

    def _store(
        self, lookup: ZoneLookup, discovery: Discovery, template: str | None, now: datetime
    ) -> None:
        if lookup.error_code is not None:
            code, message = lookup.error_code, lookup.message
        elif discovery.error_code not in (None, "no_record"):
            code, message = discovery.error_code, discovery.message
        else:
            code, message = None, None
        transient = lookup.transient or discovery.transient
        values = {
            "provider": identify(lookup, discovery).id,
            "nameservers": list(lookup.nameservers) or None,
            "domain_connect": discovery.settings,
            "template": template,
            "template_supported": discovery.template_supported if template else None,
            "error_code": code,
            "message": (message or "")[:500] or None,
            "checked_at": now,
            "expires_at": now + (RETRY_TTL if transient else ZONE_TTL),
        }
        for _attempt in range(2):
            with self.session_factory() as session:
                row = session.scalar(select(DnsZone).where(DnsZone.zone == lookup.zone))
                if row is None:
                    session.add(DnsZone(zone=lookup.zone, **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                try:
                    session.commit()
                    return
                except IntegrityError:  # another worker inserted it first: update theirs
                    session.rollback()
        raise RuntimeError(f"could not store zone {lookup.zone}")


# --- usage counts ----------------------------------------------------------------


@dataclass(frozen=True)
class SetupCount:
    provider_id: str | None
    provider_name: str | None
    method: str | None
    domains: int
    ready: int


def setup_counts(
    session: Session,
    *,
    application: Application | None = None,
    since: datetime | None = None,
    settings: SetupSettings | None = None,
) -> list[SetupCount]:
    """Live domains per detected provider and setup method, with how many are ready.

    No hostnames: only ids and counts. ``application`` limits it to one
    application; ``since`` to domains registered at or after that time.
    Domains not yet looked at are counted with provider and method None.
    """
    if settings is None:
        from app.services.domain_connect import effective_setup_settings

        settings = effective_setup_settings(session)
    query = (
        select(
            DnsZone.provider,
            DnsZone.template_supported,
            DnsZone.template,
            Domain.status,
            func.count(Domain.id),
        )
        .select_from(Domain)
        .outerjoin(DnsZone, DnsZone.zone == Domain.zone)
        .where(Domain.deleted_at.is_(None))
        .group_by(DnsZone.provider, DnsZone.template_supported, DnsZone.template, Domain.status)
    )
    if application is not None:
        query = query.where(Domain.application_id == application.id)
    if since is not None:
        query = query.where(Domain.created_at >= since)
    totals: dict[tuple[str | None, str | None], list[int]] = {}
    for provider_id, supported, template, status, count in session.execute(query):
        if provider_id is None:
            key = (None, None)
        else:
            key = (provider_id, select_method(provider_id, supported, template, settings))
        bucket = totals.setdefault(key, [0, 0])
        bucket[0] += count
        if status == DomainStatus.READY:
            bucket[1] += count
    rows = [
        SetupCount(
            provider_id=provider_id,
            provider_name=providers.get_provider(provider_id).name if provider_id else None,
            method=method,
            domains=domains,
            ready=ready,
        )
        for (provider_id, method), (domains, ready) in totals.items()
    ]
    rows.sort(key=lambda row: (-row.domains, row.provider_id or "~", row.method or "~"))
    return rows


def purge_zones(session: Session, *, now: datetime | None = None) -> int:
    """Remove cached zones that expired long ago and no domain row refers to."""
    from sqlalchemy import delete

    now = now or utcnow()
    referenced = select(Domain.id).where(Domain.zone == DnsZone.zone).exists()
    result = session.execute(
        delete(DnsZone)
        .where(DnsZone.expires_at <= now - ZONE_RETENTION, ~referenced)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0
