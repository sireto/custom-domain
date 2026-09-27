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
        "public_api": False,
    }
    client.app.state.edge_settings = SETTINGS.__class__(**{**SETTINGS.__dict__, "public_api": True})
    origins = client.get("/internal/edge/origins", headers={EDGE_TOKEN_HEADER: SETTINGS.edge_token})
    assert origins.json()["public_api"] is True
    client.app.state.edge_settings = SETTINGS

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


def test_gateway_accepts_only_the_reconcilers_api_route(session, make_application):
    import copy

    from app.edge.config import api_route
    from app.edge.gateway import EdgeFacts

    acme = make_application("acme", cname_target="acme.edge.example.net")
    _ready_domain(session, acme)
    public = SETTINGS.__class__(**{**SETTINGS.__dict__, "public_api": True})
    apps = build_apps(session, public)
    routes = apps["http"]["servers"]["edge"]["routes"]
    assert [r["@id"] for r in routes[:3]] == ["edge-health", "api", "app-acme"]
    api = routes[1]
    assert api["match"] == [{"host": ["acme.edge.example.net"], "path": ["/v1", "/v1/*"]}]
    assert api["handle"][0]["request"]["delete"] == list(edge_config.STRIPPED_REQUEST_HEADERS)
    assert api["handle"][1]["upstreams"] == [{"dial": SETTINGS.assert_upstream}]

    facts = EdgeFacts(
        upstreams=ALLOWED, edge_names=frozenset({"acme.edge.example.net"}), public_api=True
    )
    validate_apps(apps, SETTINGS, facts)  # judged by the API's facts, not the edge's env
    with pytest.raises(ConfigRejected):  # the API does not say it is public
        validate_apps(apps, SETTINGS, EdgeFacts(upstreams=ALLOWED, edge_names=facts.edge_names))
    with pytest.raises(ConfigRejected):  # a host the API does not vouch for
        validate_apps(apps, SETTINGS, EdgeFacts(upstreams=ALLOWED, public_api=True))
    for mutate in (
        lambda r: r[1]["match"][0].__setitem__("path", ["/*"]),  # the whole API, /internal too
        lambda r: r[1]["match"][0]["host"].append("forms.customer.example"),
        lambda r: r[1]["handle"][1]["upstreams"].__setitem__(0, {"dial": "evil:1"}),
        lambda r: r[1]["handle"].pop(0),  # no longer strips the edge-only headers
    ):
        bad = copy.deepcopy(apps)
        mutate(bad["http"]["servers"]["edge"]["routes"])
        with pytest.raises(ConfigRejected):
            validate_apps(bad, SETTINGS, facts)

    # Off by default: no route, and the portal and API can both be on.
    assert api_route(SETTINGS, ["acme.edge.example.net"]) == []
    both = SETTINGS.__class__(**{**public.__dict__, "portal_allowed_ips": ("203.0.113.9",)})
    both_apps = build_apps(session, both)
    ids = [r["@id"] for r in both_apps["http"]["servers"]["edge"]["routes"]]
    assert ids[:4] == ["edge-health", "portal", "portal-denied", "api"]
    validate_apps(
        both_apps,
        SETTINGS,
        EdgeFacts(
            upstreams=ALLOWED,
            edge_names=facts.edge_names,
            portal_ranges=("203.0.113.9/32",),
            public_api=True,
        ),
    )


def test_v1_access_log_names_the_real_client_behind_the_edge(
    client, session, make_application, caplog
):
    import logging

    acme = make_application("acme")
    from app.services.applications import issue_credential

    credential, secret = issue_credential(session, acme, label="backend")
    session.commit()
    auth = {"Authorization": f"Bearer {secret}"}
    from app.observability import install_log_redaction

    install_log_redaction()  # as in production: the lines pass through the redactor
    with caplog.at_level(logging.INFO, logger="app.v1.access"):
        # From the edge (a trusted peer): the forwarded address is the client.
        edge = TestClient(client.app, client=("127.0.0.1", 1000))
        assert (
            edge.get("/v1/domains", headers={**auth, "X-Forwarded-For": "8.8.8.8"}).status_code
            == 200
        )
        # From anyone else the header is ignored.
        other = TestClient(client.app, client=("10.1.2.3", 1000))
        assert other.get("/v1/domains", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 401
    lines = [r.getMessage() for r in caplog.records if r.name == "app.v1.access"]
    # The exact credential id survives redaction, so the line says which key it was
    # (`custom-domain credential revoke --id <id>` takes it).
    expected = f"GET /v1/domains 200 application=acme credential={credential.id} client=8.8.8.8"
    assert expected in lines, lines
    assert "GET /v1/domains 401 application=- credential=- client=10.1.2.3" in lines, lines
    assert all(secret not in line and "***" not in line for line in lines), lines


def test_failed_authentication_is_throttled_per_client_before_any_lookup(
    client, session, make_application, monkeypatch
):
    from app.services.applications import issue_credential
    from app.v1 import deps
    from app.v1.throttle import FailedAuthLimiter

    acme = make_application("acme")
    _, secret = issue_credential(session, acme, label="backend")
    session.commit()
    client.app.state.v1_auth_limiter = FailedAuthLimiter(max_failures=3)
    lookups = []
    real = deps.authenticate_credential
    monkeypatch.setattr(
        deps, "authenticate_credential", lambda db, s: (lookups.append(s), real(db, s))[1]
    )
    edge = TestClient(client.app, client=("127.0.0.1", 1000))  # a trusted edge peer
    attacker = {"X-Forwarded-For": "8.8.8.8"}
    bad = {**attacker, "Authorization": "Bearer cd_wrong_wrong_wrong_wrong_wrong_wrong0"}
    for _ in range(2):
        assert edge.get("/v1/domains", headers=bad).status_code == 401
    assert edge.get("/v1/domains", headers=attacker).status_code == 401  # no key counts too
    refused = edge.get("/v1/domains", headers=bad)
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1
    assert refused.json()["error"]["code"] == "rate_limited"
    assert len(lookups) == 2  # the refused request never reached the credential lookup
    # Even a valid key is refused from that address until the window passes ...
    good = {"Authorization": f"Bearer {secret}"}
    assert edge.get("/v1/domains", headers={**attacker, **good}).status_code == 429
    # ... while other clients are unaffected.
    assert (
        edge.get("/v1/domains", headers={"X-Forwarded-For": "1.1.1.1", **good}).status_code == 200
    )
    assert edge.get("/v1/domains", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 401


def test_failed_auth_is_not_limited_for_a_shared_private_peer(client, session, make_application):
    """Behind the operator's own proxy every application shares its address."""
    from app.services.applications import issue_credential
    from app.v1.throttle import FailedAuthLimiter

    acme = make_application("acme")
    _, secret = issue_credential(session, acme, label="backend")
    session.commit()
    client.app.state.v1_auth_limiter = FailedAuthLimiter(max_failures=3)
    proxy = TestClient(client.app, client=("10.1.2.3", 1000))  # a private, untrusted peer
    bad = {"Authorization": "Bearer cd_wrong_wrong_wrong_wrong_wrong_wrong0"}
    for _ in range(10):  # a worker retrying with a revoked key
        assert proxy.get("/v1/domains", headers=bad).status_code == 401
    assert (
        proxy.get("/v1/domains", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    )
    # A forwarded header from an untrusted peer is ignored, so it cannot be used
    # to put someone else's address on the limit either.
    spoof = {**bad, "X-Forwarded-For": "8.8.8.8"}
    for _ in range(5):
        assert proxy.get("/v1/domains", headers=spoof).status_code == 401
    assert client.app.state.v1_auth_limiter.tracked() == 0
    # A public address connecting directly does identify a client.
    public = TestClient(client.app, client=("9.9.9.9", 1000))
    for _ in range(3):
        public.get("/v1/domains", headers=bad)
    assert public.get("/v1/domains", headers=bad).status_code == 429


def test_failed_auth_limiter_groups_ipv6_and_bounds_its_memory():
    from app.v1.throttle import PREFIX48_FACTOR, FailedAuthLimiter, buckets

    assert buckets("2001:db8:1:2:3:4:5:6")[0] == buckets("2001:db8:1:2:ffff::1")[0]  # same /64
    assert buckets("2001:db8:1:2::1")[0] != buckets("2001:db8:1:3::1")[0]
    assert buckets("2001:db8:1:2::1")[1] == buckets("2001:db8:1:3::1")[1]  # same /48
    assert buckets("8.8.8.8") == [("8.8.8.8", 1)]

    limiter = FailedAuthLimiter(max_failures=2, window=60, max_tracked=100)
    limiter.record_failure("8.8.8.8", now=0)
    limiter.record_failure("8.8.8.8", now=1)
    assert limiter.retry_after("8.8.8.8", now=2) == 59
    assert limiter.retry_after("9.9.9.9", now=2) == 0
    assert limiter.retry_after("8.8.8.8", now=61) == 0  # the window has passed

    # Rotating through the /64s of one /48 runs into the /48 budget.
    rotating = FailedAuthLimiter(max_failures=2, window=60)
    for i in range(2 * PREFIX48_FACTOR):
        rotating.record_failure(f"2001:db8:1:{i:x}::1", now=0)
    assert rotating.retry_after("2001:db8:1:ffff::1", now=1) > 0
    assert rotating.retry_after("2001:db8:2::1", now=1) == 0

    # Address rotation cannot grow the table; the bucket that failed least
    # recently is evicted first, the active ones stay.
    for i in range(1000):
        limiter.record_failure(f"10.0.{i // 256}.{i % 256}", now=100 + i * 0.001)
    limiter.record_failure("8.8.8.8", now=101.5)
    limiter.record_failure("8.8.8.8", now=101.6)
    assert limiter.tracked() <= 100
    assert limiter.retry_after("8.8.8.8", now=102) > 0
    assert FailedAuthLimiter(max_failures=0).retry_after("8.8.8.8") == 0  # 0 disables it


def test_service_logs_reach_stderr_in_a_fresh_process(tmp_path):
    """The containers configure no logging themselves: INFO records must not be dropped."""
    import subprocess
    import sys

    code = (
        "import logging\n"
        "from app.main import create_app\n"
        "create_app()\n"
        "logging.getLogger('app.v1.access').info('GET /v1/domains 200 client=8.8.8.8')\n"
        "logging.getLogger('httpx').info('HTTP Request: GET https://example')\n"
    )
    env = {
        **__import__("os").environ,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'x.db'}",
        "ENABLE_LEGACY_API": "false",
        "EDGE_RECONCILE_ENABLED": "false",
    }
    env.pop("LOG_LEVEL", None)
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "INFO app.v1.access: GET /v1/domains 200 client=8.8.8.8" in result.stderr
    assert "HTTP Request" not in result.stderr  # client libraries stay at WARNING


def test_doctor_fails_an_acme_email_lets_encrypt_refuses():
    """A reserved contact domain made issuance impossible while the doctor said OK."""
    from app.dns.settings import DnsSettings
    from app.services.doctor import _settings_findings, reserved_email_domain

    for bad in ("ops@example.net", "a@EXAMPLE.com", "x@mail.example.org", "a@b.test", "nobody"):
        assert reserved_email_domain(bad), bad
    for good in ("ops@sireto.com", "a@example-corp.io", "x@mail.examples.net"):
        assert not reserved_email_domain(good), good
    public = SETTINGS.__class__(
        **{
            **SETTINGS.__dict__,
            "tls_issuer": "acme",
            "disable_https": False,
            "acme_email": "ops@example.net",
        }
    )
    (https,) = [f for f in _settings_findings(public, DnsSettings()) if f.check == "https"]
    assert https.status == "fail" and "invalidContact" in https.detail
    not_an_address = SETTINGS.__class__(**{**public.__dict__, "acme_email": "nobody"})
    (https,) = [f for f in _settings_findings(not_an_address, DnsSettings()) if f.check == "https"]
    assert https.status == "fail" and "is not an email address" in https.detail
    assert "reserved" not in https.detail
    fine = SETTINGS.__class__(**{**public.__dict__, "acme_email": "ops@sireto.com"})
    (https,) = [f for f in _settings_findings(fine, DnsSettings()) if f.check == "https"]
    assert https.status == "ok"
