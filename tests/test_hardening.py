import json
import logging
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.edge import config as edge_config
from app.edge.config import EDGE_TOKEN_HEADER, build_apps
from app.edge.gateway import ConfigRejected, create_gateway_app, validate_apps
from app.edge.settings import EdgeSettings
from app.models import CheckStatus, CheckType, DomainStatus
from app.models.types import utcnow
from app.observability import redact
from app.services.applications import (
    activate_origin,
    record_origin_verification,
    register_origin,
)
from app.services.domains import (
    REGISTRATION_MAX_PER_WINDOW,
    claim_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)
from app.services.errors import RateLimited

SETTINGS = EdgeSettings(
    reconcile_enabled=True,
    legacy_api_enabled=False,
    assertion_keys=(("1", "k" * 40),),
    edge_token="t" * 40,
    ask_trusted_hosts=("10.90.0.0/16", "127.0.0.1", "::1", "testclient"),
)


# --- name validation and registration limits --------------------------------------


def test_registration_budget_is_per_application(session, make_application, monkeypatch):
    from app.services import domains as domain_service

    monkeypatch.setattr(domain_service, "REGISTRATION_MAX_PER_WINDOW", 2)
    acme = make_application("acme")
    globex = make_application("globex")
    t0 = utcnow()
    claim_domain(session, acme, "a.customer.example", "w", now=t0)
    claim_domain(session, acme, "b.customer.example", "w", now=t0)
    with pytest.raises(RateLimited) as info:
        claim_domain(session, acme, "c.customer.example", "w", now=t0 + timedelta(seconds=1))
    assert info.value.retry_after >= 3500
    claim_domain(session, globex, "g.customer.example", "w", now=t0)  # own budget
    claim_domain(session, acme, "c.customer.example", "w", now=t0 + timedelta(hours=1, seconds=1))
    assert REGISTRATION_MAX_PER_WINDOW >= 60


# --- trust and tokens -------------------------------------------------------------


def test_settings_trust_addresses_and_networks():
    assert (
        SETTINGS.trusts("10.90.3.4")
        and SETTINGS.trusts("127.0.0.1")
        and SETTINGS.trusts("testclient")
    )
    assert (
        not SETTINGS.trusts("10.91.0.1") and not SETTINGS.trusts(None) and not SETTINGS.trusts("")
    )
    settings = EdgeSettings.from_env(
        {
            "ENABLE_LEGACY_API": "false",
            "EDGE_ASSERTION_KEYS": "1:" + "k" * 32,
            "EDGE_ASK_TRUSTED_HOSTS": "10.0.0.0/8, ::1",
            "EDGE_TOKEN": "abc",
        }
    )
    assert settings.trusts("10.200.1.1") and settings.edge_token == "abc"


@pytest.fixture
def client(session_factory, monkeypatch):
    from app.db.session import get_session
    from app.main import create_app

    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    app = create_app()

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        c.app.state.edge_settings = SETTINGS
        yield c


def _ready_domain(session, application, hostname="forms.customer.example"):
    origin = register_origin(session, application, host="app.acme.example")
    record_origin_verification(session, origin, verified=True)
    activate_origin(session, origin)
    domain = claim_domain(session, application, hostname, "ws_1")
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)
    session.commit()
    return domain


def test_edge_token_required_when_configured(client, session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme)
    headers = {"X-Custom-Domain-Edge-Host": "forms.customer.example"}
    assert client.get("/internal/edge/assert", headers=headers).status_code == 403
    assert (
        client.get(
            "/internal/edge/assert", headers={**headers, EDGE_TOKEN_HEADER: "wrong"}
        ).status_code
        == 403
    )
    ok = client.get(
        "/internal/edge/assert", headers={**headers, EDGE_TOKEN_HEADER: SETTINGS.edge_token}
    )
    assert ok.status_code == 200

    assert client.get("/internal/edge/origins").status_code == 403
    origins = client.get("/internal/edge/origins", headers={EDGE_TOKEN_HEADER: SETTINGS.edge_token})
    assert origins.status_code == 200 and origins.json() == {
        "upstreams": [{"dial": "203.0.113.10:443", "host": "app.acme.example"}],
        "edge_names": ["acme.edge.example.net"],
        "portal_ranges": [],
    }

    # The ask endpoint cannot carry headers: address trust only.
    assert (
        client.get("/internal/tls/ask", params={"domain": "forms.customer.example"}).status_code
        == 200
    )
    apps = build_apps(session, SETTINGS)
    subrequest = [r for r in apps["http"]["servers"]["edge"]["routes"] if r["@id"] == "app-acme"][
        0
    ]["handle"][1]
    assert subrequest["headers"]["request"]["set"][EDGE_TOKEN_HEADER] == [SETTINGS.edge_token]
    assert EDGE_TOKEN_HEADER in edge_config.STRIPPED_REQUEST_HEADERS


def test_metrics_endpoint_exposes_counters_and_gauges(client, session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme)
    assert client.get("/internal/metrics").status_code == 403
    response = client.get("/internal/metrics", headers={EDGE_TOKEN_HEADER: SETTINGS.edge_token})
    assert response.status_code == 200
    text = response.text
    assert 'custom_domain_domains{status="ready"} 1.0' in text
    assert 'custom_domain_domains{status="pending_dns"} 0.0' in text
    assert "custom_domain_checks_total" in text and "custom_domain_status_transitions_total" in text
    assert (
        "custom_domain_dns_check_seconds" in text
        and "custom_domain_webhook_deliveries_total" in text
    )
    assert "cd_" not in text and "whsec_" not in text


# --- log redaction ------------------------------------------------------------------


def test_secrets_are_redacted_from_logs(caplog):
    from app.observability import install_log_redaction

    install_log_redaction()
    logger = logging.getLogger("app.test.redaction")
    with caplog.at_level(logging.INFO):
        logger.info("credential cd_%s issued", "a" * 43)
        logger.info("hook secret whsec_%s; token custom-domain-verify=%s", "b" * 40, "c" * 40)
        logger.info(
            "assertion v1.1.%s.%s password=hunter2 Authorization: Bearer xyz", "p" * 20, "s" * 40
        )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("a" * 43, "b" * 40, "c" * 40, "s" * 40, "hunter2", "xyz"):
        assert secret not in joined, secret
    assert "cd_***" in joined and "whsec_***" in joined
    assert redact("nothing secret here") == "nothing secret here"


# --- configuration gateway -----------------------------------------------------------


@pytest.fixture
def valid_apps(session, make_application):
    acme = make_application("acme")
    _ready_domain(session, acme)
    return build_apps(session, SETTINGS)


# The gateway learns {pinned address: origin name} from /internal/edge/origins.
ALLOWED = {"203.0.113.10:443": "app.acme.example"}


def test_gateway_accepts_reconciler_output_only(valid_apps):
    validate_apps(valid_apps, SETTINGS, ALLOWED)
    import copy

    def mutated(fn):
        apps = copy.deepcopy(valid_apps)
        fn(apps)
        return apps

    server = lambda a: a["http"]["servers"]["edge"]  # noqa: E731
    app_route = lambda a: [r for r in server(a)["routes"] if r["@id"].startswith("app-")][0]  # noqa: E731
    cases = {
        "file server": lambda a: app_route(a)["handle"].__setitem__(
            2, {"handler": "file_server", "root": "/var/lib/custom-domain"}
        ),
        "extra handler": lambda a: app_route(a)["handle"].append(
            {"handler": "static_response", "body": "x"}
        ),
        "unknown upstream": lambda a: app_route(a)["handle"][2]["upstreams"].__setitem__(
            0, {"dial": "evil.example:443"}
        ),
        "extra listener": lambda a: server(a).__setitem__("listen", [":443", ":8443"]),
        "second server": lambda a: a["http"]["servers"].__setitem__("other", {"listen": [":8080"]}),
        "no health route": lambda a: server(a)["routes"].pop(0),
        "no fallback": lambda a: server(a)["routes"].pop(),
        "insecure transport": lambda a: app_route(a)["handle"][2].__setitem__(
            "transport", {"protocol": "http", "tls": {"insecure_skip_verify": True}}
        ),
        "server name of another origin": lambda a: app_route(a)["handle"][2].__setitem__(
            "transport", {"protocol": "http", "tls": {"server_name": "evil.example"}}
        ),
        "tampered assert step": lambda a: app_route(a)["handle"][1]["upstreams"].__setitem__(
            0, {"dial": "evil:1"}
        ),
        "storage in apps": lambda a: a.__setitem__("storage", {"module": "file_system"}),
        "tls policy change": lambda a: a["tls"]["automation"]["policies"].append(
            {"issuers": [{"module": "internal"}]}
        ),
        "wildcard host": lambda a: app_route(a)["match"][0]["host"].append("*.customer.example"),
        "duplicate host": lambda a: app_route(a)["match"][0]["host"].append(
            "forms.customer.example"
        ),
        "extra header": lambda a: app_route(a)["handle"][2]["headers"]["request"][
            "set"
        ].__setitem__("X-Evil", ["1"]),
        "no strip": lambda a: app_route(a)["handle"].__setitem__(
            0, {"handler": "headers", "request": {"delete": ["X-Other"]}}
        ),
    }
    for name, fn in cases.items():
        with pytest.raises(ConfigRejected):
            validate_apps(mutated(fn), SETTINGS, ALLOWED)
        assert name
    with pytest.raises(ConfigRejected):
        validate_apps("nope", SETTINGS, ALLOWED)
    local = EdgeSettings(**{**SETTINGS.__dict__, "disable_https": True})
    with pytest.raises(ConfigRejected):
        validate_apps(valid_apps, local, ALLOWED)  # tls block not allowed without https


def test_gateway_app_forwards_only_valid_configs(valid_apps):
    caddy_calls = []
    running = {
        "admin": {"listen": "x"},
        "storage": {"password": "secret"},
        "apps": {"http": {"servers": {}}},
    }

    def fake_caddy(request: httpx.Request) -> httpx.Response:
        caddy_calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/config/apps":
            return httpx.Response(200, json=running["apps"])
        if request.method == "POST" and request.url.path == "/config/apps":
            running["apps"] = json.loads(request.content)
            return httpx.Response(200)
        return httpx.Response(404)

    origins = {"upstreams": dict(ALLOWED)}
    app = create_gateway_app(
        SETTINGS,
        caddy_admin_url="http://caddy",
        origins_provider=lambda: origins["upstreams"],
        transport=httpx.MockTransport(fake_caddy),
    )
    app_client = TestClient(app)

    assert app_client.get("/config/").json() == {"apps": running["apps"]}
    assert "storage" not in app_client.get("/config/").json()
    assert app_client.post("/config/apps", json=valid_apps).status_code == 200
    assert running["apps"] == valid_apps and ("POST", "/config/apps") in caddy_calls

    bad = json.loads(json.dumps(valid_apps))
    bad["http"]["servers"]["edge"]["routes"][1]["handle"][2] = {
        "handler": "file_server",
        "root": "/",
    }
    response = app_client.post("/config/apps", json=bad)
    assert response.status_code == 400 and "file_server" not in json.dumps(running["apps"])
    assert app_client.post("/load", json=valid_apps).status_code in (404, 405)
    assert app_client.get("/config/storage").status_code == 404

    origins["upstreams"] = {}
    assert (
        app_client.post("/config/apps", json=valid_apps).status_code == 400
    )  # upstream no longer verified

    def broken():
        raise RuntimeError("api down")

    app2 = create_gateway_app(SETTINGS, caddy_admin_url="http://caddy", origins_provider=broken)
    assert TestClient(app2).post("/config/apps", json=valid_apps).status_code == 503


def test_gateway_accepts_only_the_reconcilers_portal_routes(session, make_application):
    import copy

    from app.edge.config import build_apps, portal_routes
    from app.edge.gateway import ConfigRejected, EdgeFacts, validate_apps

    acme = make_application("acme", cname_target="acme.edge.example.net")
    _ready_domain(session, acme)
    exposed = SETTINGS.__class__(
        **{**SETTINGS.__dict__, "portal_allowed_ips": ("203.0.113.9", "198.51.100.0/24")}
    )
    apps = build_apps(session, exposed)
    routes = apps["http"]["servers"]["edge"]["routes"]
    assert [r["@id"] for r in routes[:3]] == ["edge-health", "portal", "portal-denied"]
    assert routes[1]["match"][0]["remote_ip"]["ranges"] == ["203.0.113.9/32", "198.51.100.0/24"]
    assert routes[1]["match"][0]["host"] == ["acme.edge.example.net"]

    ranges = ("203.0.113.9/32", "198.51.100.0/24")
    facts = EdgeFacts(
        upstreams=ALLOWED, edge_names=frozenset({"acme.edge.example.net"}), portal_ranges=ranges
    )
    # The gateway validates against what the API states, not its own environment:
    # the edge container needs no PORTAL_ALLOWED_IPS and no restart to change it.
    validate_apps(apps, SETTINGS, facts)
    validate_apps(apps, exposed, facts)

    with pytest.raises(ConfigRejected):  # the API exposes no portal: routes refused
        validate_apps(apps, SETTINGS, EdgeFacts(upstreams=ALLOWED, edge_names=facts.edge_names))
    with pytest.raises(ConfigRejected):  # host the API does not vouch for
        validate_apps(apps, exposed, EdgeFacts(upstreams=ALLOWED, portal_ranges=ranges))
    with pytest.raises(ConfigRejected):  # a different allowlist than the API states
        validate_apps(
            apps,
            exposed,
            EdgeFacts(
                upstreams=ALLOWED, edge_names=facts.edge_names, portal_ranges=("203.0.113.9/32",)
            ),
        )
    for mutate in (
        lambda a: a["http"]["servers"]["edge"]["routes"][1]["match"][0]["remote_ip"][
            "ranges"
        ].append("0.0.0.0/0"),
        lambda a: a["http"]["servers"]["edge"]["routes"][1]["handle"][0]["upstreams"].__setitem__(
            0, {"dial": "evil:1"}
        ),
        lambda a: a["http"]["servers"]["edge"]["routes"][1]["match"][0].__setitem__("path", ["/*"]),
        lambda a: a["http"]["servers"]["edge"]["routes"].pop(2),
    ):
        bad = copy.deepcopy(apps)
        mutate(bad)
        with pytest.raises(ConfigRejected):
            validate_apps(bad, exposed, facts)

    # Without an allowlist the reconciler emits no portal routes.
    plain = build_apps(session, SETTINGS)
    assert [r["@id"] for r in plain["http"]["servers"]["edge"]["routes"]][:2] == [
        "edge-health",
        "app-acme",
    ]
    assert portal_routes(SETTINGS, ["acme.edge.example.net"]) == []
