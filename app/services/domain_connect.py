"""One-step DNS setup with Domain Connect: the signer and signed apply URLs.

See docs/domain-connect.md. The pieces:

* **The signer configuration.** ``DOMAIN_CONNECT_SIGNER*`` settings, unless
  the operator set a remote signer through the operator API, the CLI or the
  portal (``set_remote_signer``); that row then overrides the settings and
  the template ids until ``reset_signer``. Every signing request reads it
  afresh, so a change takes effect at once on every API instance.
* **Apply URLs** (``prepare_connect``, ``sign_connect``, ``record_connect``).
  The URL is signed **outside any database transaction**: a remote signer
  checks the domain by calling this deployment's ``GET /v1/domains/{id}``
  while it answers, and must never wait on a lock this request holds.
* **The return.** With a return URL configured, the provider sends the
  customer to ``/v1/domain-connect/return`` with a sealed ``state`` naming
  the domain; ``connect_returned`` makes its DNS checks due at once, and the
  customer goes on to the application's own ``redirect_uri``. Whatever the
  redirect says, only the DNS checks decide anything.
* **The fast recheck.** For ``FAST_RECHECK_WINDOW`` after a URL is issued or
  the customer returns, failing DNS checks run every
  ``FAST_RECHECK_INTERVAL`` instead of backing off (app/services/dns_checks.py).

Never logged: the private key, the credential, the query (it carries the
challenge token and the application's state) and the signature.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from app.dns import signing
from app.dns.settings import SetupSettings, SignerSettings
from app.models import (
    Application,
    CheckType,
    Domain,
    DomainConnectSigner,
    DomainEvent,
    DomainStatus,
    EventType,
)
from app.models.types import utcnow
from app.services import secret_box
from app.services.errors import (
    DomainConnectSignerUnavailable,
    DomainConnectUnavailable,
    InvalidConnectRequest,
    InvalidSignerConfig,
    RateLimited,
)

logger = logging.getLogger(__name__)
audit = logging.getLogger("app.domain_connect.audit")

ACTIVE = "active"
CREDENTIAL_PURPOSE = "domain-connect-credential"
STATE_PURPOSE = "domain-connect-state"
RETURN_PATH = "/v1/domain-connect/return"

# Statuses a domain may be connected in (the remote signer accepts the same).
CONNECTABLE = frozenset(
    {
        DomainStatus.PENDING_DNS,
        DomainStatus.PROVISIONING,
        DomainStatus.READY,
        DomainStatus.ATTENTION_REQUIRED,
    }
)
# How long the sealed return state is accepted; also the URL's ``expires_at``.
URL_TTL = timedelta(hours=1)
FAST_RECHECK_WINDOW = timedelta(minutes=30)
FAST_RECHECK_INTERVAL = timedelta(seconds=30)
# Issued URLs, counted from events: per domain and per application.
DOMAIN_WINDOW = timedelta(hours=1)
DOMAIN_MAX_PER_WINDOW = 10
APPLICATION_WINDOW = timedelta(hours=1)
APPLICATION_MAX_PER_WINDOW = 120
# What an application may pass through the provider.
MAX_REDIRECT_URI = 512
MAX_STATE = 512
DNS_CHECKS = (CheckType.OWNERSHIP, CheckType.ROUTING)


# --- configuration -------------------------------------------------------------------


@lru_cache(maxsize=1)
def signer_settings() -> SignerSettings:
    """The deployment's ``DOMAIN_CONNECT_SIGNER*`` settings, read once."""
    return SignerSettings.from_env()


@lru_cache(maxsize=4)
def _local_signer(private_key: str, key_host: str | None) -> signing.LocalSigner:
    return signing.LocalSigner(signing.parse_private_keys(private_key, key_host))


@dataclass(frozen=True)
class SignerConfig:
    """The signer in effect: the operator's row if set, else the settings."""

    signer: str | None
    source: str  # "operator" or "settings"
    provider_id: str
    service_id: str
    url: str | None = None
    credential_prefix: str | None = None
    updated_at: datetime | None = None
    settings: SignerSettings = field(default_factory=SignerSettings, repr=False)
    ciphertext: str | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        if not self.provider_id:
            return False
        if self.source == "operator":
            return bool(self.ciphertext)
        return self.settings.enabled


def _row(session: Session) -> DomainConnectSigner | None:
    return session.get(DomainConnectSigner, ACTIVE, populate_existing=True)


def signer_config(session: Session, *, fresh: bool = False) -> SignerConfig:
    """The configuration in effect, read once per session unless ``fresh``."""
    cached = session.info.get("domain_connect_signer")
    if cached is not None and not fresh:
        return cached
    from app.services.dns_setup import setup_settings

    base = setup_settings()
    row = _row(session)
    if row is not None:
        config = SignerConfig(
            signer=row.signer,
            source="operator",
            provider_id=row.provider_id,
            service_id=row.service_id,
            url=row.url,
            credential_prefix=row.credential_prefix,
            updated_at=row.updated_at,
            ciphertext=row.credential_ciphertext,
        )
    else:
        settings = signer_settings()
        config = SignerConfig(
            signer=settings.signer,
            source="settings",
            provider_id=base.template_provider_id,
            service_id=base.template_service_id,
            settings=settings,
        )
    session.info["domain_connect_signer"] = config
    return config


def effective_setup_settings(session: Session | None) -> SetupSettings:
    """``SetupSettings`` with the operator's signer and template applied, if set."""
    from app.services.dns_setup import setup_settings

    base = setup_settings()
    if session is None:
        return base
    config = signer_config(session)
    if config.source != "operator":
        return base
    return replace(
        base,
        template_provider_id=config.provider_id,
        template_service_id=config.service_id,
        signing_enabled=config.enabled,
    )


def settings_for(domain: Domain) -> SetupSettings:
    """The effective settings for ``domain``, from the session it was loaded in."""
    return effective_setup_settings(object_session(domain))


@dataclass(frozen=True)
class SignerView:
    """What ``GET /operator/v1/domain-connect/signer`` shows: never the credential."""

    signer: str | None
    url: str | None
    credential_prefix: str | None
    provider_id: str | None
    service_id: str | None
    updated_at: datetime | None
    source: str
    enabled: bool
    problem: str | None = None


def signer_view(session: Session) -> SignerView:
    config = signer_config(session, fresh=True)
    if config.source != "operator":
        return SignerView(
            signer=config.signer,
            url=None,
            credential_prefix=None,
            provider_id=None,
            service_id=None,
            updated_at=None,
            source="settings",
            enabled=config.enabled,
        )
    prefix, problem = config.credential_prefix, None
    try:
        secret_box.decrypt(config.ciphertext or "", CREDENTIAL_PURPOSE)
    except (secret_box.Undecryptable, secret_box.SecretKeyMissing):
        # Reported as not held, so whoever manages the signer sets it again.
        prefix = None
        problem = "The stored credential cannot be decrypted with SERVICE_SECRET_KEY; set it again"
    return SignerView(
        signer=config.signer,
        url=config.url,
        credential_prefix=prefix,
        provider_id=config.provider_id,
        service_id=config.service_id,
        updated_at=config.updated_at,
        source="operator",
        enabled=config.enabled and problem is None,
        problem=problem,
    )


def set_remote_signer(
    session: Session,
    *,
    url: str,
    credential: str,
    provider_id: str,
    service_id: str,
    now: datetime | None = None,
) -> DomainConnectSigner:
    """Store a remote signer; it overrides the settings until ``reset_signer``.

    The credential is encrypted before it is stored and never returned.
    One row is updated in place, so the switch is atomic at commit.
    """
    url = url.strip()
    problem = signing.https_url_problem(url) if url else "is required"
    if problem or len(url) > 512:
        raise InvalidSignerConfig(f"url {problem or 'is too long'}", details={"field": "url"})
    if not signing.CREDENTIAL.match(credential):
        raise InvalidSignerConfig(
            "credential must be dcs_ followed by 43 letters, digits, - or _",
            details={"field": "credential"},
        )
    for name, value in (("provider_id", provider_id), ("service_id", service_id)):
        if not signing.IDENTIFIER.match(value or ""):
            raise InvalidSignerConfig(
                f"{name} must be 1 to 64 letters, digits, dots, hyphens or underscores",
                details={"field": name},
            )
    ciphertext = secret_box.encrypt(credential, CREDENTIAL_PURPOSE)
    now = now or utcnow()
    row = _row(session)
    if row is None:
        row = DomainConnectSigner(name=ACTIVE)
        session.add(row)
    row.signer = "remote"
    row.url = url
    row.credential_ciphertext = ciphertext
    row.credential_prefix = credential[: signing.CREDENTIAL_PREFIX_LENGTH]
    row.provider_id = provider_id
    row.service_id = service_id
    row.updated_at = now
    session.flush()
    session.info.pop("domain_connect_signer", None)
    logger.info("domain connect signer set: remote %s for %s/%s", url, provider_id, service_id)
    return row


def reset_signer(session: Session) -> bool:
    """Forget the operator's signer, so the settings apply again."""
    row = _row(session)
    session.info.pop("domain_connect_signer", None)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    logger.info("domain connect signer reset to the settings")
    return True


def make_signer(config: SignerConfig, *, transport=None):
    """The signer for ``config``; raises ``DomainConnectSignerUnavailable``."""
    if config.source == "operator":
        try:
            credential = secret_box.decrypt(config.ciphertext or "", CREDENTIAL_PURPOSE)
        except (secret_box.Undecryptable, secret_box.SecretKeyMissing):
            logger.error(
                "domain connect: the stored signer credential cannot be decrypted; "
                "set the signer again"
            )
            raise DomainConnectSignerUnavailable(
                "The Domain Connect signer is misconfigured; the operator must set it again"
            ) from None
        return signing.RemoteSigner(
            config.url or "",
            credential,
            provider_id=config.provider_id,
            service_id=config.service_id,
            transport=transport,
        )
    settings = config.settings
    if settings.signer == "remote":
        return signing.RemoteSigner(
            settings.url or "",
            settings.credential or "",
            provider_id=config.provider_id,
            service_id=config.service_id,
            transport=transport,
        )
    if settings.signer == "local" and settings.private_key:
        try:
            return _local_signer(settings.private_key, settings.key_host)
        except signing.InvalidSigningKey as exc:
            logger.error("domain connect: %s", exc)
            raise DomainConnectSignerUnavailable(
                "The Domain Connect signer is misconfigured"
            ) from None
    raise DomainConnectUnavailable("Domain Connect is not configured on this deployment")


# --- apply URLs ----------------------------------------------------------------------


def redirect_uri_problem(value: str) -> str | None:
    if len(value.encode("utf-8")) > MAX_REDIRECT_URI:
        return f"must be at most {MAX_REDIRECT_URI} bytes"
    return signing.https_url_problem(value)


def state_problem(value: str) -> str | None:
    if len(value.encode("utf-8")) > MAX_STATE:
        return f"must be at most {MAX_STATE} bytes"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "must not contain control characters"
    return None


@dataclass(frozen=True)
class ConnectPlan:
    """Everything needed to sign, copied out of the database before signing."""

    domain_id: uuid.UUID
    application_id: uuid.UUID
    application_slug: str
    claim_token: str = field(repr=False)
    zone: str = ""
    host: str = ""
    target: str = ""
    url_sync_ux: str = ""
    provider_id: str | None = None
    provider_name: str | None = None
    config: SignerConfig | None = field(default=None, repr=False)
    query: str = field(default="", repr=False)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class ConnectUrl:
    url: str = field(repr=False)
    provider_id: str | None
    provider_name: str | None
    expires_at: datetime


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _enforce_limits(session: Session, domain: Domain, now: datetime) -> None:
    issued = DomainEvent.event_type == EventType.CONNECT_URL_ISSUED.value
    for scope, condition, window, limit in (
        ("domain", DomainEvent.domain_id == domain.id, DOMAIN_WINDOW, DOMAIN_MAX_PER_WINDOW),
        (
            "application",
            DomainEvent.application_id == domain.application_id,
            APPLICATION_WINDOW,
            APPLICATION_MAX_PER_WINDOW,
        ),
    ):
        filters = [issued, condition, DomainEvent.created_at > now - window]
        count = session.scalar(select(func.count()).select_from(DomainEvent).where(*filters))
        if count is not None and count >= limit:
            oldest = _as_utc(
                session.scalar(select(func.min(DomainEvent.created_at)).where(*filters))
            )
            wait = (oldest + window - now).total_seconds() if oldest else 60
            raise RateLimited(
                f"At most {limit} Domain Connect links per {scope} per hour",
                retry_after=math.ceil(wait),
            )


def prepare_connect(
    session: Session,
    application: Application,
    domain_id: uuid.UUID,
    *,
    redirect_uri: str | None = None,
    state: str | None = None,
    return_url: str | None = None,
    now: datetime | None = None,
) -> ConnectPlan:
    """Check that the domain can be connected and build the query to sign. Reads only.

    With ``return_url`` (and ``SERVICE_SECRET_KEY``), the provider sends the
    customer back to it with a sealed state naming the domain, the
    application's ``redirect_uri`` and ``state`` and an expiry; otherwise
    ``redirect_uri`` and ``state`` go to the provider as given.
    """
    from app.services.dns_setup import DOMAIN_CONNECT, domain_setup
    from app.services.domains import get_domain

    now = now or utcnow()
    if redirect_uri is not None and (problem := redirect_uri_problem(redirect_uri)):
        raise InvalidConnectRequest(f"redirect_uri {problem}", details={"field": "redirect_uri"})
    if state is not None and (problem := state_problem(state)):
        raise InvalidConnectRequest(f"state {problem}", details={"field": "state"})
    domain = get_domain(session, application, domain_id)
    if domain.status not in CONNECTABLE:
        raise DomainConnectUnavailable(
            f"A domain in status {domain.status.value} cannot be connected",
            details={"status": domain.status.value},
        )
    settings = effective_setup_settings(session)
    setup = domain_setup(domain, settings)
    zone = domain.dns_zone if domain.zone else None
    url_sync_ux = (zone.domain_connect or {}).get("url_sync_ux") if zone is not None else None
    claim = domain.active_claim
    if setup.method != DOMAIN_CONNECT or not url_sync_ux or claim is None:
        raise DomainConnectUnavailable(
            "One-step setup is not available for this domain; use setup.guide_url",
            details={"method": setup.method},
        )
    _enforce_limits(session, domain, now)
    expires_at = now + URL_TTL
    params = [
        ("domain", setup.zone),
        ("host", setup.host),
        ("target", claim.cname_target),
        ("token", claim.token),
    ]
    if return_url and secret_box.available():
        sealed = secret_box.seal(
            {
                "d": str(domain.id),
                "e": int(expires_at.timestamp()),
                "r": redirect_uri,
                "s": state,
            },
            STATE_PURPOSE,
        )
        params += [("redirect_uri", return_url), ("state", sealed)]
    else:
        if redirect_uri is not None:
            params.append(("redirect_uri", redirect_uri))
        if state is not None:
            params.append(("state", state))
    return ConnectPlan(
        domain_id=domain.id,
        application_id=application.id,
        application_slug=application.slug,
        claim_token=claim.token,
        zone=setup.zone,
        host=setup.host,
        target=claim.cname_target,
        url_sync_ux=url_sync_ux,
        provider_id=setup.provider_id,
        provider_name=setup.provider_name,
        config=signer_config(session),
        query=signing.canonical_query(params),
        expires_at=expires_at,
    )


def sign_connect(plan: ConnectPlan, *, transport=None, signer=None) -> str:
    """Sign ``plan``'s query and return the apply URL. Call it outside any transaction."""
    config = plan.config
    if config is None or not config.enabled:
        raise DomainConnectUnavailable("Domain Connect is not configured on this deployment")
    signer = signer or make_signer(config, transport=transport)
    try:
        signature = signer.sign(
            plan.query, application=plan.application_slug, domain_id=str(plan.domain_id)
        )
    except signing.SigningFailed as exc:
        logger.warning(
            "domain connect: signing refused for domain %s (%s: %s)",
            plan.domain_id,
            exc.kind,
            exc.code,
        )
        if exc.kind == "rate_limited":
            raise RateLimited(
                "The signer is rate limiting Domain Connect links; retry later",
                retry_after=exc.retry_after or 60,
            ) from None
        if exc.kind == "refused":
            raise DomainConnectUnavailable(
                "The signer refused to sign a Domain Connect link for this domain",
                details={"reason": exc.code},
            ) from None
        raise DomainConnectSignerUnavailable(
            "The Domain Connect signer is unavailable; try again shortly",
            details={"reason": exc.code},
        ) from None
    return signing.apply_url(
        plan.url_sync_ux, config.provider_id, config.service_id, plan.query, signature
    )


def make_checks_due(domain: Domain, at: datetime) -> None:
    for check in domain.checks:
        if check.check_type in DNS_CHECKS:
            current = _as_utc(check.next_check_at)
            if current is None or current > at:
                check.next_check_at = at


def record_connect(
    session: Session, application: Application, plan: ConnectPlan, url: str, *, now=None
) -> ConnectUrl:
    """Audit the issued URL and start the fast recheck. The caller commits."""
    from app.services.domains import get_domain, lock_application, record_event

    now = now or utcnow()
    # The first statement of this transaction (see lock_application).
    lock_application(session, plan.application_id)
    domain = get_domain(session, application, plan.domain_id)
    claim = domain.active_claim
    if claim is None or claim.token != plan.claim_token:
        raise DomainConnectUnavailable(
            "The domain's DNS records changed while the link was signed; request a new one"
        )
    domain.connect_started_at = now
    make_checks_due(domain, now + FAST_RECHECK_INTERVAL)
    record_event(
        session,
        domain,
        EventType.CONNECT_URL_ISSUED,
        {"provider": plan.provider_id, "signer": plan.config.signer if plan.config else None},
        now=now,
    )
    audit.info(
        "domain connect link issued: application %s domain %s provider %s",
        plan.application_slug,
        domain.id,
        plan.provider_id,
    )
    return ConnectUrl(url, plan.provider_id, plan.provider_name, plan.expires_at or now)


def default_return_url(edge_settings) -> str | None:
    """``DOMAIN_CONNECT_RETURN_URL``, else this service's public API on the edge hostname."""
    configured = signer_settings().return_url
    if configured:
        return configured
    if edge_settings is not None and edge_settings.public_api and edge_settings.edge_hostname:
        return f"https://{edge_settings.edge_hostname}{RETURN_PATH}"
    return None


# --- the return ----------------------------------------------------------------------


@dataclass(frozen=True)
class Return:
    domain_id: uuid.UUID
    redirect_uri: str | None
    state: str | None
    expired: bool


def open_return_state(value: str, *, now: datetime | None = None) -> Return | None:
    """The sealed return state, or None if this service did not make it."""
    if not secret_box.available():
        return None
    data = secret_box.unseal(value, STATE_PURPOSE)
    if data is None:
        return None
    try:
        domain_id = uuid.UUID(str(data["d"]))
        expires = int(data["e"])
    except (KeyError, TypeError, ValueError):
        return None
    redirect_uri = data.get("r") if isinstance(data.get("r"), str) else None
    state = data.get("s") if isinstance(data.get("s"), str) else None
    now = now or utcnow()
    return Return(domain_id, redirect_uri, state, now.timestamp() > expires)


def connect_returned(session: Session, domain_id: uuid.UUID, *, now=None) -> bool:
    """The customer came back from their provider: check the domain's DNS now.

    Never trusts the redirect: it only makes the DNS checks due and extends
    the fast recheck; the checks decide. The caller commits.
    """
    from app.services.domains import _domain_query, lock_application

    now = now or utcnow()
    application_id = session.scalar(select(Domain.application_id).where(Domain.id == domain_id))
    session.rollback()
    if application_id is None:
        return False
    # The first statement of the transaction (see lock_application).
    lock_application(session, application_id)
    domain = session.scalar(_domain_query(include_deleted=False).where(Domain.id == domain_id))
    if domain is None or domain.status not in CONNECTABLE:
        return False
    domain.connect_started_at = now
    make_checks_due(domain, now)
    session.flush()
    audit.info("domain connect return for domain %s", domain.id)
    return True


def fast_recheck_due(domain: Domain, now: datetime) -> bool:
    started = _as_utc(domain.connect_started_at)
    return started is not None and timedelta(0) <= now - started < FAST_RECHECK_WINDOW


def back_to_application(redirect_uri: str, state: str | None, error: str | None) -> str:
    """``redirect_uri`` with the application's ``state`` (and the provider's ``error``)."""
    parts = urlsplit(redirect_uri)
    extra = []
    if state is not None:
        extra.append(("state", state))
    if error is not None:
        extra.append(("error", error))
    if not extra:
        return redirect_uri
    added = urlencode(extra, quote_via=quote)
    query = f"{parts.query}&{added}" if parts.query else added
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
