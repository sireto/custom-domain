"""Plain-language views of the service's state for the portal.

Templates never interpret raw statuses or check codes: everything the operator
reads (what a status means, which DNS record is present, what to do next) is
decided here, from the same rows the API and the workers use, so the pages
stay consistent with the lifecycle rules in docs/lifecycle.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models import (
    ApiCredential,
    Application,
    ApplicationStatus,
    CheckStatus,
    CheckType,
    Domain,
    DomainEvent,
    DomainStatus,
    OriginStatus,
    VerifiedOrigin,
)
from app.models.types import utcnow

# Tones map to the portal's status marks (an 8px square and the word, never
# colour alone): ok (green, filled: ready), progress (amber, half filled: under
# way), pending (amber, outlined: waiting on someone), warn (amber, filled),
# fail (red, filled: not served or broken), muted (neutral, outlined: retired).


@dataclass(frozen=True)
class State:
    label: str
    tone: str
    help: str = ""


DOMAIN_STATES: dict[DomainStatus, State] = {
    DomainStatus.PENDING_DNS: State(
        "Waiting for DNS",
        "pending",
        "The customer has not published both DNS records below yet, or they have not "
        "propagated. Nothing is served for this hostname until they have.",
    ),
    DomainStatus.PROVISIONING: State(
        "Provisioning",
        "progress",
        "Both DNS records are correct. The edge is obtaining the HTTPS certificate and "
        "checking that the origin serves this workspace; this usually takes a few minutes.",
    ),
    DomainStatus.READY: State(
        "Live",
        "ok",
        "Every check passes and the edge serves this hostname over HTTPS.",
    ),
    DomainStatus.ATTENTION_REQUIRED: State(
        "Needs attention",
        "fail",
        "A check started failing after the hostname went live, so the edge has stopped "
        "serving it. It goes live again by itself once every check passes; the failing "
        "check below says what to fix.",
    ),
    DomainStatus.SUSPENDED: State(
        "Suspended",
        "fail",
        "The ownership TXT record was missing for more than 24 hours, so service stopped. "
        "It resumes once the customer restores the TXT record and both DNS checks pass.",
    ),
    DomainStatus.DELETING: State(
        "Deleted",
        "muted",
        "The hostname was removed and is no longer served. The record is kept for 90 days "
        "for auditing; the hostname can be registered again at any time.",
    ),
}

# The order statuses are offered in filters: the ones needing action first.
DOMAIN_FILTERS: list[tuple[str, str]] = [
    ("", "All"),
    (DomainStatus.ATTENTION_REQUIRED.value, "Needs attention"),
    (DomainStatus.SUSPENDED.value, "Suspended"),
    (DomainStatus.PENDING_DNS.value, "Waiting for DNS"),
    (DomainStatus.PROVISIONING.value, "Provisioning"),
    (DomainStatus.READY.value, "Live"),
    (DomainStatus.DELETING.value, "Deleted"),
]

CHECK_LABELS: dict[CheckType, tuple[str, str]] = {
    CheckType.OWNERSHIP: (
        "Ownership",
        "The TXT record proves the customer controls the hostname.",
    ),
    CheckType.ROUTING: (
        "Routing",
        "The CNAME record sends the hostname's traffic to this edge.",
    ),
    CheckType.CERTIFICATE: (
        "HTTPS certificate",
        "The edge holds a valid certificate for the hostname.",
    ),
    CheckType.ORIGIN: (
        "Origin",
        "The application's backend answers for the hostname with the right workspace.",
    ),
}

APPLICATION_STATES: dict[ApplicationStatus, State] = {
    ApplicationStatus.ACTIVE: State("Active", "ok"),
    ApplicationStatus.SUSPENDED: State(
        "Suspended",
        "fail",
        "None of this application's hostnames is served and its API keys are refused.",
    ),
}

ORIGIN_STATES: dict[OriginStatus, State] = {
    OriginStatus.PENDING: State(
        "Waiting for verification",
        "pending",
        "Serve the token below from the origin, then verify.",
    ),
    OriginStatus.VERIFIED: State("Verified", "ok"),
    OriginStatus.FAILED: State(
        "Verification failed",
        "fail",
        "The origin did not serve the expected token. Fix it and verify again.",
    ),
    OriginStatus.RETIRED: State("Retired", "muted", "No longer receives traffic."),
}


def domain_state(domain: Domain) -> State:
    return DOMAIN_STATES[domain.status]


def application_state(application: Application) -> State:
    if application.is_deleted:
        return State(
            "Deleted",
            "muted",
            "Read only. Its records are kept until "
            + application.purge_after.strftime("%Y-%m-%d")
            + " for auditing, then purged.",
        )
    return APPLICATION_STATES[application.status]


HOST_HEADER_MODES = {
    "customer": (
        "Customer hostname",
        "The edge sends Host: the customer's hostname, such as forms.customer.example.",
    ),
    "origin": (
        "Origin's own name",
        "The edge sends Host: this origin's own name, so a CDN or a host-routed proxy in front "
        "of it routes the request as usual; the customer's hostname is in X-Forwarded-Host.",
    ),
}


def host_header_mode(origin: VerifiedOrigin) -> tuple[str, str]:
    """The Host the edge sends this origin, as a label and an explanation."""
    return HOST_HEADER_MODES[origin.host_header.value]


def origin_state(origin: VerifiedOrigin) -> State:
    if origin.is_active and origin.status == OriginStatus.VERIFIED:
        return State("Active", "ok", "Receives this application's traffic.")
    if origin.status == OriginStatus.VERIFIED:
        return State("Verified, not active", "progress", "Activate it to send traffic here.")
    return ORIGIN_STATES[origin.status]


def credential_state(credential: ApiCredential, now: datetime | None = None) -> State:
    now = now or utcnow()
    if credential.revoked_at is not None:
        return State("Revoked", "muted")
    if credential.expires_at is not None and credential.expires_at <= now:
        return State("Expired", "muted")
    if credential.expires_at is not None:
        return State("Active until " + credential.expires_at.strftime("%Y-%m-%d %H:%M"), "warn")
    return State("Active", "ok")


CREDENTIAL_SCOPES = {
    "full": (
        "Full access",
        "Can do everything the API allows for this application: register, recheck and "
        "delete hostnames, and manage webhooks.",
    ),
    "read": (
        "Read only",
        "Can list and look at hostnames, their checks and webhooks, and nothing else. "
        "Any request that would change something is refused (credential_read_only).",
    ),
}


def credential_scope(credential: ApiCredential) -> tuple[str, str]:
    """What a credential may do, as a label and an explanation."""
    return CREDENTIAL_SCOPES[credential.scope.value]


# --- checks and DNS records ---------------------------------------------------------


@dataclass(frozen=True)
class CheckView:
    type: CheckType
    label: str
    purpose: str
    state: State
    message: str | None
    error_code: str | None
    observed_at: datetime | None
    next_check_at: datetime | None


def check_views(domain: Domain) -> list[CheckView]:
    by_type = {check.check_type: check for check in domain.checks}
    dns_passing = all(
        by_type.get(t) is not None and by_type[t].status == CheckStatus.PASSING
        for t in (CheckType.OWNERSHIP, CheckType.ROUTING)
    )
    views = []
    for check_type in CheckType:
        label, purpose = CHECK_LABELS[check_type]
        check = by_type.get(check_type)
        status = check.status if check else CheckStatus.PENDING
        if status == CheckStatus.PASSING:
            state = State("Passing", "ok")
        elif status == CheckStatus.FAILING:
            state = State("Failing", "fail")
        elif check_type in (CheckType.CERTIFICATE, CheckType.ORIGIN) and not dns_passing:
            state = State("Waits for DNS", "pending")
        else:
            state = State("Not checked yet", "pending")
        views.append(
            CheckView(
                type=check_type,
                label=label,
                purpose=purpose,
                state=state,
                message=check.message if check else None,
                error_code=check.error_code if check else None,
                observed_at=check.observed_at if check else None,
                next_check_at=check.next_check_at if check else None,
            )
        )
    return views


@dataclass(frozen=True)
class RecordView:
    purpose: str
    type: str
    name: str
    host_label: str | None  # the name as most DNS providers want it (relative to the zone)
    zone: str | None
    value: str
    state: State
    detail: str | None
    observed: list[str] = field(default_factory=list)
    help: str = ""


_MISSING = {"txt_record_not_found", "cname_not_found"}
_WRONG = {"txt_token_mismatch", "txt_token_stale", "cname_target_mismatch"}


def _record_state(check, *, kind: str) -> tuple[State, str | None, list[str]]:
    if check is None or check.status == CheckStatus.PENDING:
        return State("Not checked yet", "pending"), None, []
    details = check.details or {}
    observed = [str(v) for v in details.get("observed") or details.get("chain") or []]
    if check.status == CheckStatus.PASSING:
        return State("Found", "ok"), None, observed
    code = check.error_code or ""
    if code in _MISSING:
        return State("Not found", "fail"), check.message, observed
    if code in _WRONG:
        return State("Wrong value", "fail"), check.message, observed
    if code == "dns_timeout":
        return State("Lookup failed", "warn"), check.message, observed
    return State("Failing", "fail"), check.message, observed


def _zone(hostname: str) -> str | None:
    """The registrable domain of ``hostname`` per the public suffix list, if any."""
    from app.hostname import _PSL

    return _PSL.privatesuffix(hostname)


def _domain_zone(domain: Domain) -> str | None:
    """The zone the DNS worker found for the domain, else the registrable domain."""
    return domain.zone or _zone(domain.hostname)


def _relative(name: str, hostname: str, zone: str | None = None) -> str | None:
    """The record name relative to the customer's zone, or None when that is unclear.

    DNS providers ask for the part before the zone (``_custom-domain-challenge.forms``
    for ``forms.customer.example``). The zone is taken to be the registrable
    domain from the public suffix list (``customer.co.uk`` for
    ``forms.customer.co.uk``); a customer whose zone is delegated further down
    still has the full name, which is always shown. The DNS worker replaces
    that guess with the zone it found (``zone``).
    """
    zone = zone or _zone(hostname)
    if zone is None:
        return None
    if name == zone:
        return "@"
    if name.endswith("." + zone):
        return name[: -(len(zone) + 1)]
    return None


def record_views(domain: Domain) -> list[RecordView]:
    from app.v1.schemas import CNAME_HELP, TXT_HELP

    claim = domain.active_claim
    if claim is None or domain.is_deleted:
        return []
    ownership, detail_txt, observed_txt = _record_state(
        domain.check(CheckType.OWNERSHIP), kind="txt"
    )
    routing, detail_cname, observed_cname = _record_state(
        domain.check(CheckType.ROUTING), kind="cname"
    )
    return [
        RecordView(
            purpose="Proves ownership",
            type="TXT",
            name=claim.txt_record_name,
            host_label=_relative(claim.txt_record_name, domain.hostname, _domain_zone(domain)),
            zone=_domain_zone(domain),
            value=claim.txt_record_value,
            state=ownership,
            detail=detail_txt,
            observed=observed_txt,
            help=TXT_HELP.replace("`", ""),
        ),
        RecordView(
            purpose="Routes traffic to the edge",
            type="CNAME",
            name=domain.hostname,
            host_label=_relative(domain.hostname, domain.hostname, _domain_zone(domain)),
            zone=_domain_zone(domain),
            value=claim.cname_target,
            state=routing,
            detail=detail_cname,
            observed=observed_cname,
            help=CNAME_HELP.replace("`", ""),
        ),
    ]


# --- DNS provider and setup method -----------------------------------------------

SETUP_METHODS: dict[str | None, State] = {
    "domain_connect": State(
        "One-step setup",
        "ok",
        "The customer's DNS provider supports this service's Domain Connect template: the "
        "customer approves the records at their provider instead of typing them.",
    ),
    "guided": State(
        "Provider guide",
        "progress",
        "The customer adds the records by hand, following the step-by-step guide for their "
        "DNS provider.",
    ),
    "manual": State(
        "Generic guide",
        "pending",
        "The DNS provider was not recognized or has no specific guide; the customer follows "
        "the generic instructions.",
    ),
    None: State(
        "Not looked up yet",
        "muted",
        "The DNS worker identifies the provider the next time it checks this hostname.",
    ),
}


@dataclass(frozen=True)
class SetupView:
    state: State
    provider_name: str | None
    provider_id: str | None
    zone: str
    host: str
    guide_url: str | None
    guide_label: str
    detected_at: datetime | None
    nameservers: list[str]
    domain_connect: str
    problem: str | None


def setup_method_state(method: str | None) -> State:
    return SETUP_METHODS.get(method, SETUP_METHODS[None])


def _domain_connect_text(zone, settings) -> tuple[str, str | None]:
    """What Domain Connect discovery found, in words, and the problem if it failed."""
    if zone is None:
        return "Not checked yet", None
    problem = None
    if zone.error_code:
        problem = f"{zone.message or 'Lookup failed'} ({zone.error_code})"
    offered = zone.domain_connect is not None
    if zone.template_supported:
        if settings.signing_enabled:
            return "Supported, including this service's template", problem
        return (
            "Supports this service's template; one-step setup needs request signing, "
            "which this deployment does not have",
            problem,
        )
    if offered and zone.template_supported is False:
        return "Offered, but not for this service's template", problem
    if offered and not zone.template:
        return "Offered (no template configured: DOMAIN_CONNECT_PROVIDER_ID)", problem
    if offered:
        return "Offered; template support unknown", problem
    if problem is not None:
        return "Could not be checked", problem
    return "Not offered by this provider", None


def setup_view(domain: Domain) -> SetupView:
    from app.dns.providers import get_provider
    from app.services.dns_setup import domain_setup
    from app.services.domain_connect import settings_for

    settings = settings_for(domain)
    setup = domain_setup(domain, settings)
    zone = domain.dns_zone if domain.zone else None
    dc_text, problem = _domain_connect_text(zone, settings)
    return SetupView(
        state=setup_method_state(setup.method),
        provider_name=setup.provider_name,
        provider_id=setup.provider_id,
        zone=setup.zone,
        host=setup.host,
        guide_url=setup.guide_url,
        guide_label=(
            f"{get_provider(setup.provider_id).name} setup guide"
            if get_provider(setup.provider_id).guide
            else "Generic setup guide"
        ),
        detected_at=setup.detected_at,
        nameservers=list(zone.nameservers or []) if zone is not None else [],
        domain_connect=dc_text,
        problem=problem,
    )


# What a v1 caller sees when one-step setup cannot be offered, in words.
DOMAIN_CONNECT_ERRORS = {
    "domain_connect_unavailable": "One-step setup is not available for this hostname: its DNS "
    "provider does not support the template, no signer is configured, the hostname is not "
    "live, or the signer refused it. The customer follows the setup guide instead.",
    "domain_connect_signer_unavailable": "The signer could not be reached or answered badly. "
    "The application can ask again shortly.",
    "service_secret_key_missing": "SERVICE_SECRET_KEY is not set, so the signer's credential "
    "cannot be stored encrypted. Add a random value of at least 32 characters to .env.",
}


def signer_state(view) -> State:
    """The Domain Connect signer, in words."""
    if view.problem:
        return State("Misconfigured", "fail", view.problem)
    if view.signer is None:
        return State("Off", "muted", "No signer: one-step setup is never offered.")
    if not view.enabled:
        return State(
            "Incomplete",
            "warn",
            "A signer is chosen but the template id (DOMAIN_CONNECT_PROVIDER_ID) or its key "
            "or credential is missing.",
        )
    label = "Remote signing service" if view.signer == "remote" else "Local key"
    return State(label, "ok", "Customers whose provider supports the template get a one-step link.")


def setup_count_views(rows) -> list[tuple[str, State, int, int]]:
    """Usage counts as (provider name, method state, domains, ready) for the overview."""
    return [
        (
            row.provider_name or "Not looked up yet",
            setup_method_state(row.method),
            row.domains,
            row.ready,
        )
        for row in rows
    ]


def dns_summary(domain: Domain) -> State:
    """One line for lists: are the customer's DNS records in place?"""
    records = record_views(domain)
    if not records:
        return State("—", "muted")
    tones = [r.state.tone for r in records]
    if all(t == "ok" for t in tones):
        return State("Records found", "ok")
    if any(t == "fail" for t in tones):
        missing = [r.type for r in records if r.state.tone == "fail"]
        return State(" and ".join(missing) + " missing or wrong", "fail")
    return State("Not checked yet", "pending")


# --- setup progress ---------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    title: str
    done: bool
    detail: str
    link: str | None = None
    link_label: str | None = None


def application_steps(
    application: Application,
    *,
    origins: list[VerifiedOrigin],
    credentials: list[ApiCredential],
    domain_counts: dict[DomainStatus, int],
    target_dns: EdgeNameView | None,
) -> list[Step]:
    base = f"/portal/applications/{application.slug}"
    now = utcnow()
    registered = any(o.status != OriginStatus.RETIRED for o in origins)
    serving = application.serving_origin
    has_key = any(c.is_usable(now) for c in credentials)
    live_total = sum(n for s, n in domain_counts.items() if s != DomainStatus.DELETING)
    ready = domain_counts.get(DomainStatus.READY, 0)
    target = application.cname_target
    reach = target_dns.reachability if target_dns is not None else None
    if target_dns is None or target_dns.error or not target_dns.addresses:
        dns_done = False
        dns_detail = (
            f"{target} has no A or AAAA record in public DNS yet. Create them with this "
            "server's public addresses; customers' CNAMEs point at this name."
        )
    elif reach is None:
        dns_done = False
        dns_detail = (
            f"{target} resolves to {', '.join(target_dns.addresses)}. Verify that those "
            "addresses reach this edge."
        )
    elif reach.status in ("ok", "warn") and reach.reached:
        # Done only when at least one address actually answered as this edge.
        dns_done = True
        if reach.status == "ok":
            dns_detail = f"{target} reaches this edge at {', '.join(reach.reached)}."
        else:
            dns_detail = (
                f"{target} reaches this edge at {', '.join(reach.reached)}. Its IPv6 "
                "address could not be checked from here; test it from outside."
            )
    elif reach.status == "warn":
        dns_done = False
        dns_detail = (
            f"{target} has only IPv6 addresses ({', '.join(target_dns.addresses)}), which "
            "cannot be checked from inside Docker. Add an A record for this server, or "
            "verify IPv6 from outside."
        )
    else:
        dns_done = False
        dns_detail = (
            f"{target} resolves to {', '.join(target_dns.addresses)}, but those addresses do "
            "not answer as this edge. Point the records at this server's public addresses."
        )
    return [
        Step(
            "Point the CNAME target at this edge",
            dns_done,
            dns_detail,
            f"{base}?verify=1" if target_dns and target_dns.addresses else "/portal/edge",
            "Verify now" if target_dns and target_dns.addresses else "DNS for the edge",
        ),
        Step(
            "Register the application's backend (origin)",
            registered,
            "The server the edge forwards customers' requests to."
            if not registered
            else "Registered.",
            f"{base}/origins",
            "Origins",
        ),
        Step(
            "Verify and activate the origin",
            serving is not None,
            f"Traffic goes to {serving.url}."
            if serving
            else "The origin serves a token to prove you control it; then it is activated.",
            f"{base}/origins",
            "Origins",
        ),
        Step(
            "Issue an API key for the application's backend",
            has_key,
            "The backend registers customers' hostnames with it through the API or SDK."
            if not has_key
            else "At least one active key.",
            f"{base}/credentials",
            "API keys",
        ),
        Step(
            "Register the first customer hostname",
            live_total > 0,
            "Usually done by the application through the API; you can also add one here."
            if not live_total
            else f"{live_total} hostname(s) registered.",
            f"{base}/domains",
            "Domains",
        ),
        Step(
            "First hostname live",
            ready > 0,
            f"{ready} hostname(s) live."
            if ready
            else "Goes live once the customer's DNS records are in place and every check passes.",
            f"{base}/domains?status=ready" if ready else f"{base}/domains",
            "Domains",
        ),
    ]


# --- the edge's own names ---------------------------------------------------------


@dataclass
class EdgeNameView:
    name: str
    roles: list[str]
    addresses: list[str] = field(default_factory=list)
    error: str | None = None
    # Filled in only when the operator asks for the reachability check.
    reachability: Any = None

    @property
    def state(self) -> State:
        if self.error:
            return State("Not in DNS", "fail")
        if not self.addresses:
            return State("No A/AAAA record", "fail")
        if self.reachability is not None:
            status = self.reachability.status
            if status == "ok":
                return State("Reaches this edge", "ok")
            if status == "warn" and self.reachability.reached:
                return State("Reaches this edge over IPv4", "warn")
            if status == "warn":
                return State("Not verifiable from here", "warn")
            return State("Does not reach this edge", "fail")
        return State("Resolves, not verified", "progress")

    @property
    def ipv4(self) -> list[str]:
        return [a for a in self.addresses if ":" not in a]

    @property
    def ipv6(self) -> list[str]:
        return [a for a in self.addresses if ":" in a]


def edge_names(session, settings) -> list[EdgeNameView]:
    """Every name the edge answers for, with why: its own name and each CNAME target."""
    from app.services.applications import edge_names as application_edge_names
    from app.services.applications import list_applications

    views: dict[str, EdgeNameView] = {}
    if settings is not None and settings.edge_hostname:
        views[settings.edge_hostname] = EdgeNameView(
            settings.edge_hostname, ["This edge's own name (portal and health check)"]
        )
    for application in list_applications(session):
        for name in application_edge_names(session, application):
            role = (
                f"CNAME target of {application.name}"
                if name == application.cname_target
                else f"Former CNAME target of {application.name}, still used by live domains"
            )
            views.setdefault(name, EdgeNameView(name, [])).roles.append(role)
    return list(views.values())


def resolve_names(views: list[EdgeNameView], resolve, dns_settings) -> None:
    for view in views:
        try:
            view.addresses = list(resolve(view.name, dns_settings))
        except Exception as exc:  # the resolver raises LookupError for NXDOMAIN
            view.error = str(exc) or type(exc).__name__


# --- events -----------------------------------------------------------------------

_EVENT_TEXT = {
    "domain.created": "Hostname registered",
    "domain.imported": "Imported from the legacy deployment",
    "domain.claim_issued": "DNS records issued",
    "domain.claim_verified": "Ownership verified",
    "domain.claim_revoked": "Previous DNS records withdrawn",
    "domain.check_updated": "Check updated",
    "domain.status_changed": "Status changed",
    "domain.deleted": "Hostname deleted",
    "domain.recheck_requested": "Recheck requested",
    "domain.connect_url_issued": "One-step setup link issued (Domain Connect)",
}


def _status_label(value: str | None) -> str:
    try:
        return DOMAIN_STATES[DomainStatus(value)].label
    except (ValueError, KeyError):
        return value or "?"


def describe_event(event: DomainEvent) -> tuple[str, str]:
    """A title and a one-line detail for an event."""
    payload = event.payload or {}
    title = _EVENT_TEXT.get(event.event_type, event.event_type)
    detail = ""
    if event.event_type == "domain.status_changed":
        detail = f"{_status_label(payload.get('from'))} → {_status_label(payload.get('to'))}"
        if payload.get("reason"):
            detail += f" ({str(payload['reason']).replace('_', ' ')})"
    elif event.event_type == "domain.check_updated":
        try:
            label = CHECK_LABELS[CheckType(payload.get("check"))][0]
        except (ValueError, KeyError):
            label = str(payload.get("check"))
        to = payload.get("to")
        title = f"{label} check {'passing' if to == 'passing' else to or 'updated'}"
        if payload.get("error_code"):
            detail = str(payload["error_code"]).replace("_", " ")
    elif event.event_type == "domain.claim_verified":
        method = payload.get("method")
        detail = {"dns_txt": "by the TXT record", "legacy_import": "by legacy import"}.get(
            method, method or ""
        )
    elif event.event_type == "domain.claim_revoked":
        detail = str(payload.get("reason", "")).replace("_", " ")
    elif event.event_type == "domain.created":
        detail = f"workspace {payload.get('reference')}" if payload.get("reference") else ""
    elif event.event_type == "domain.imported":
        detail = "grandfathered" if payload.get("grandfathered") else ""
    return title, detail
