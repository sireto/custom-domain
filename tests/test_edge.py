import threading

import pytest

from app.edge.caddy_client import CaddyRejectedConfig, CaddyUnavailable
from app.edge.config import (
    build_apps,
    build_bootstrap,
    build_caddy_config,
    config_digest,
    hostnames_in,
)
from app.edge.reconcile import Reconciler
from app.edge.settings import EdgeConfigurationError, EdgeSettings, redact
from app.models import (
    Application,
    ApplicationStatus,
    CheckStatus,
    CheckType,
    Domain,
    DomainStatus,
    OriginStatus,
    VerifiedOrigin,
)
from app.services.applications import (
    activate_origin,
    record_origin_verification,
    register_origin,
    set_application_status,
)
from app.services.domains import (
    claim_domain,
    delete_domain,
    mark_claim_verified,
    record_check,
    transition_status,
)

SETTINGS = EdgeSettings(reconcile_enabled=True, legacy_api_enabled=False)


def _make_ready(session, domain):
    mark_claim_verified(session, domain)
    for check_type in CheckType:
        record_check(session, domain, check_type, CheckStatus.PASSING)
    transition_status(session, domain, DomainStatus.PROVISIONING)
    transition_status(session, domain, DomainStatus.READY)


def _with_origin(session, application, host, scheme="https", port=None):
    origin = register_origin(session, application, host=host, scheme=scheme, port=port)
    record_origin_verification(session, origin, verified=True)
    activate_origin(session, origin)
    return origin


@pytest.fixture
def fleet(session, make_application):
    acme = make_application("acme")
    globex = make_application("globex")
    noorigin = make_application("noorigin")
    _with_origin(session, acme, "app.acme.example")
    _with_origin(session, globex, "globex.internal", scheme="http", port=8080)

    ready_b = claim_domain(session, acme, "b.customer.example", "ws_b")
    ready_a = claim_domain(session, acme, "a.customer.example", "ws_a")
    pending = claim_domain(session, acme, "pending.customer.example", "ws_p")
    gone = claim_domain(session, acme, "gone.customer.example", "ws_g")
    drift = claim_domain(session, acme, "drift.customer.example", "ws_d")
    g1 = claim_domain(session, globex, "one.globex-customer.example", "g_1")
    n1 = claim_domain(session, noorigin, "one.noorigin-customer.example", "n_1")
    for domain in (ready_a, ready_b, gone, drift, g1, n1):
        _make_ready(session, domain)
    delete_domain(session, acme, gone.id)
    record_check(session, drift, CheckType.ROUTING, CheckStatus.FAILING, error_code="cname_gone")
    session.commit()
    return {"acme": acme, "globex": globex, "noorigin": noorigin, "pending": pending}


# --- config derivation ---------------------------------------------------------


def test_config_routes_only_serveable_hostnames_per_application(session, fleet):
    config = build_caddy_config(session, SETTINGS)
    server = config["apps"]["http"]["servers"]["edge"]
    assert server["listen"] == [":443"]
    assert "storage" not in config
    assert [route["@id"] for route in server["routes"]] == [
        "edge-health",
        "app-acme",
        "app-globex",
        "edge-unmatched",
    ]

    _health, acme_route, globex_route, _unmatched = server["routes"]
    assert acme_route["match"] == [{"host": ["a.customer.example", "b.customer.example"]}]
    assert acme_route["terminal"] is True
    strip, assert_step, proxy = acme_route["handle"]
    assert strip["handler"] == "headers" and assert_step["rewrite"]["uri"].endswith("/assert")
    assert proxy["handler"] == "reverse_proxy"
    assert proxy["upstreams"] == [{"dial": "app.acme.example:443"}]
    assert proxy["transport"] == {"protocol": "http", "tls": {"server_name": "app.acme.example"}}
    assert globex_route["match"] == [{"host": ["one.globex-customer.example"]}]
    assert "transport" not in globex_route["handle"][-1]
    assert globex_route["handle"][-1]["upstreams"] == [{"dial": "globex.internal:8080"}]
    assert hostnames_in(config) == {
        "a.customer.example",
        "b.customer.example",
        "one.globex-customer.example",
    }


def test_suspended_application_drops_out_of_the_config(session, fleet):
    set_application_status(session, fleet["globex"], ApplicationStatus.SUSPENDED)
    session.commit()
    assert hostnames_in(build_caddy_config(session, SETTINGS)) == {
        "a.customer.example",
        "b.customer.example",
    }


def test_config_is_deterministic_and_reflects_changes(session, fleet):
    first = build_caddy_config(session, SETTINGS)
    assert build_caddy_config(session, SETTINGS) == first
    before = config_digest(first)
    delete_domain(
        session,
        fleet["acme"],
        next(d.id for d in session.query(Domain).filter_by(hostname="a.customer.example")),
    )
    session.commit()
    after = build_caddy_config(session, SETTINGS)
    assert config_digest(after) != before
    assert "a.customer.example" not in hostnames_in(after)


def test_config_includes_storage_tls_and_https_options(session, fleet):
    settings = EdgeSettings(
        https_port=8443,
        acme_email="ops@example.net",
        storage="redis",
        redis_address=("redis-a:6379", "redis-b:6379"),
        redis_password="secret",
        redis_encryption_key="k" * 40,
        redis_tls=True,
        reconcile_enabled=True,
        legacy_api_enabled=False,
    )
    config = build_caddy_config(session, settings)
    assert config["apps"]["http"]["servers"]["edge"]["listen"] == [":8443"]
    assert config["storage"] == {
        "module": "redis",
        "client_type": "cluster",
        "address": ["redis-a:6379", "redis-b:6379"],
        "db": 0,
        "key_prefix": "caddy",
        "tls_enabled": True,
        "password": "secret",
        "encryption_key": "k" * 32,
    }
    assert config["apps"]["tls"]["automation"]["policies"][0]["issuers"] == [
        {"module": "acme", "email": "ops@example.net"}
    ]
    masked = redact(config)
    assert masked["storage"]["password"] == "***" and masked["storage"]["encryption_key"] == "***"
    assert config["storage"]["password"] == "secret"

    local = EdgeSettings(
        disable_https=True, acme_email="x@y.z", reconcile_enabled=True, legacy_api_enabled=False
    )
    config = build_caddy_config(session, local)
    assert config["apps"]["http"]["servers"]["edge"]["automatic_https"] == {"disable": True}
    assert "tls" not in config["apps"]


def test_failed_origin_recheck_stops_routing_until_reverified(session, fleet):
    acme = fleet["acme"]
    origin = acme.active_origin
    assert origin is not None and origin.status == OriginStatus.VERIFIED
    assert "a.customer.example" in hostnames_in(build_caddy_config(session, SETTINGS))

    record_origin_verification(session, origin, verified=False, error_code="tls_failed")
    session.commit()
    session.expire_all()

    assert origin.is_active is False and origin.status == OriginStatus.FAILED
    assert acme.active_origin is None and acme.serving_origin is None
    routed = hostnames_in(build_caddy_config(session, SETTINGS))
    assert "a.customer.example" not in routed and "b.customer.example" not in routed
    assert "one.globex-customer.example" in routed

    record_origin_verification(session, origin, verified=True)
    activate_origin(session, origin)
    session.commit()
    assert "a.customer.example" in hostnames_in(build_caddy_config(session, SETTINGS))


def test_builder_ignores_an_active_origin_whose_status_is_not_verified(session, fleet):
    # Defence in depth: even if a row is left active with a non-verified
    # status (for example by a direct database edit), it is not routed.
    acme = fleet["acme"]
    origin = session.get(VerifiedOrigin, acme.active_origin.id)
    origin.status = OriginStatus.FAILED
    session.commit()
    session.expire_all()
    assert acme.active_origin is not None and acme.serving_origin is None
    assert "a.customer.example" not in hostnames_in(build_caddy_config(session, SETTINGS))


def test_bootstrap_holds_admin_and_storage_and_apps_holds_routes(session, fleet):
    settings = EdgeSettings(
        admin_url="http://127.0.0.1:2019",
        storage="redis",
        redis_address=("redis:6379",),
        redis_password="pw",
        reconcile_enabled=True,
        legacy_api_enabled=False,
    )
    bootstrap = build_bootstrap(settings)
    assert bootstrap["admin"] == {"listen": "127.0.0.1:2019"}
    assert bootstrap["storage"]["password"] == "pw"
    assert [r["@id"] for r in bootstrap["apps"]["http"]["servers"]["edge"]["routes"]] == [
        "edge-health",
        "edge-unmatched",
    ]

    apps = build_apps(session, settings)
    assert "storage" not in apps and "admin" not in apps
    assert (
        len(apps["http"]["servers"]["edge"]["routes"]) == 4
    )  # health, two applications, unmatched
    full = build_caddy_config(session, settings)
    assert full["storage"] == bootstrap["storage"] and full["apps"] == apps


def test_empty_database_yields_a_valid_empty_server(session):
    config = build_caddy_config(session, SETTINGS)
    assert [r["@id"] for r in config["apps"]["http"]["servers"]["edge"]["routes"]] == [
        "edge-health",
        "edge-unmatched",
    ]


# --- settings ------------------------------------------------------------------


def test_settings_defaults_and_legacy_conflict():
    settings = EdgeSettings.from_env({})
    assert settings.legacy_api_enabled and not settings.reconcile_enabled
    assert settings.storage == "file" and settings.storage_config() is None

    settings = EdgeSettings.from_env(
        {"ENABLE_LEGACY_API": "false", "EDGE_ASSERTION_KEYS": "1:" + "k" * 32}
    )
    assert settings.reconcile_enabled

    with pytest.raises(EdgeConfigurationError, match="cannot both be true"):
        EdgeSettings.from_env({"ENABLE_LEGACY_API": "true", "EDGE_RECONCILE_ENABLED": "true"})


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"CADDY_STORAGE": "redis"}, "CADDY_REDIS_ADDRESS"),
        ({"CADDY_STORAGE": "s3"}, "CADDY_STORAGE"),
        (
            {
                "CADDY_STORAGE": "redis",
                "CADDY_REDIS_ADDRESS": "r:6379",
                "CADDY_REDIS_ENCRYPTION_KEY": "short",
            },
            "32",
        ),
        ({"EDGE_RECONCILE_INTERVAL": "0"}, "EDGE_RECONCILE_INTERVAL"),
        ({"EDGE_HTTPS_PORT": "70000"}, "EDGE_HTTPS_PORT"),
    ],
)
def test_settings_validation(env, match):
    with pytest.raises(EdgeConfigurationError, match=match):
        EdgeSettings.from_env(
            {"ENABLE_LEGACY_API": "false", "EDGE_ASSERTION_KEYS": "1:" + "k" * 32, **env}
        )


def test_settings_redis_from_env():
    settings = EdgeSettings.from_env(
        {
            "ENABLE_LEGACY_API": "false",
            "EDGE_ASSERTION_KEYS": "1:" + "k" * 32,
            "CADDY_STORAGE": "redis",
            "CADDY_REDIS_ADDRESS": "redis:6379",
            "CADDY_REDIS_PASSWORD": "pw",
            "CADDY_REDIS_TLS": "yes",
            "CADDY_REDIS_KEY_PREFIX": "edge1",
        }
    )
    assert settings.storage_config() == {
        "module": "redis",
        "client_type": "simple",
        "address": ["redis:6379"],
        "db": 0,
        "key_prefix": "edge1",
        "tls_enabled": True,
        "password": "pw",
    }


# --- reconciliation ------------------------------------------------------------


class FakeCaddy:
    """Records what the reconciler sends. ``running`` mimics Caddy's full config."""

    def __init__(self, *, running=None):
        self.running = running
        self.loads: list[dict] = []
        self.full_loads = 0
        self.fail_get = None
        self.fail_load = None

    def get_config(self):
        if self.fail_get:
            raise self.fail_get
        return self.running

    def load_config(self, config):
        if self.fail_load:
            raise self.fail_load
        self.running = config
        self.full_loads += 1
        self.loads.append(config)

    def set_apps(self, apps):
        if self.fail_load:
            raise self.fail_load
        assert self.running is not None
        self.running = {**self.running, "apps": apps}
        self.loads.append(apps)


@pytest.fixture
def reconciler(session_factory):
    caddy = FakeCaddy(running=build_bootstrap(SETTINGS))
    return Reconciler(session_factory, caddy, SETTINGS), caddy


def test_reconcile_applies_then_converges(session, fleet, reconciler):
    reconciler, caddy = reconciler
    first = reconciler.run_once()
    assert first.ok and first.changed
    assert first.routes == 2 and first.hostnames == 3
    assert len(caddy.loads) == 1 and caddy.full_loads == 0
    assert hostnames_in(caddy.running) == hostnames_in({"apps": caddy.loads[0]})
    assert "admin" in caddy.running  # bootstrap keys untouched

    second = reconciler.run_once()
    assert second.ok and not second.changed and len(caddy.loads) == 1
    assert reconciler.last_result is second

    delete_domain(
        session,
        fleet["acme"],
        next(d.id for d in session.query(Domain).filter_by(hostname="a.customer.example")),
    )
    session.commit()
    third = reconciler.run_once()
    assert third.changed and "a.customer.example" not in hostnames_in(caddy.running)


def test_rejected_or_unreachable_caddy_leaves_state_intact(session, fleet, reconciler):
    reconciler, caddy = reconciler
    assert reconciler.run_once().changed
    delete_domain(
        session,
        fleet["acme"],
        next(d.id for d in session.query(Domain).filter_by(hostname="b.customer.example")),
    )
    session.commit()
    good = caddy.running

    caddy.fail_load = CaddyRejectedConfig(400, "boom")
    result = reconciler.run_once()
    assert not result.ok and result.error == "config_rejected" and "boom" in result.detail
    assert caddy.running is good  # Caddy kept its last good config
    # The authoritative state is untouched by the failure.
    assert session.query(Domain).filter_by(hostname="b.customer.example").one().deleted_at

    caddy.fail_load = None
    caddy.fail_get = CaddyUnavailable("down")
    result = reconciler.run_once()
    assert result.error == "caddy_unavailable" and not result.changed

    caddy.fail_get = None
    result = reconciler.run_once()
    assert result.ok and result.changed
    assert "b.customer.example" not in hostnames_in(caddy.running)


def test_run_forever_runs_immediately_and_stops(session, fleet, reconciler):
    reconciler, caddy = reconciler
    stop = threading.Event()
    thread = threading.Thread(target=reconciler.run_forever, args=(stop, 60))
    thread.start()
    for _ in range(100):
        if caddy.loads:
            break
        threading.Event().wait(0.05)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(caddy.loads) == 1


def test_database_failure_is_reported_not_raised(reconciler):
    reconciler, _ = reconciler

    def broken():
        raise RuntimeError("db down")

    reconciler.session_factory = broken
    result = reconciler.run_once()
    assert result.error == "database_unavailable" and "db down" in result.detail


def test_reconcile_preserves_storage_block_and_falls_back_to_full_load(
    session, fleet, session_factory
):
    redis_settings = EdgeSettings(
        storage="redis",
        redis_address=("redis:6379",),
        redis_password="pw",
        reconcile_enabled=True,
        legacy_api_enabled=False,
    )
    caddy = FakeCaddy(running=build_bootstrap(redis_settings))
    result = Reconciler(session_factory, caddy, redis_settings).run_once()
    assert result.changed
    assert caddy.running["storage"]["password"] == "pw"
    assert "storage" not in caddy.loads[0]

    bare = FakeCaddy(running=None)  # Caddy started with no config at all
    result = Reconciler(session_factory, bare, SETTINGS).run_once()
    assert result.changed and bare.full_loads == 1
    assert set(bare.running) == {"apps"}


def test_stale_snapshot_cannot_restore_a_deleted_hostname(session, fleet, session_factory):
    """Two instances share one Caddy. Instance A builds its snapshot (hostname
    present), a deletion commits, and the delete-triggered run on instance B
    must not be overtaken by A's stale apply."""
    import time

    from app.services.domains import lock_application

    gate = threading.Event()

    class GatedCaddy(FakeCaddy):
        block_next_get = False

        def get_config(self):
            if self.block_next_get:
                self.block_next_get = False
                assert gate.wait(30), "gate never opened"
            return super().get_config()

    caddy = GatedCaddy(running=build_bootstrap(SETTINGS))
    instance_a = Reconciler(session_factory, caddy, SETTINGS)
    instance_b = Reconciler(session_factory, caddy, SETTINGS)
    assert instance_a.run_once().changed
    assert "a.customer.example" in hostnames_in(caddy.running)
    target_id = session.query(Domain).filter_by(hostname="a.customer.example").one().id
    acme_id = fleet["acme"].id

    # A: snapshot built (hostname present), lock held, stuck talking to Caddy.
    caddy.block_next_get = True
    thread_a = threading.Thread(target=instance_a.run_once)
    thread_a.start()
    for _ in range(500):
        if not caddy.block_next_get:
            break
        time.sleep(0.01)
    assert not caddy.block_next_get, "instance A never reached Caddy"

    # B: delete the hostname, then reconcile like the API's post-delete task.
    outcome = {}

    def delete_then_reconcile():
        with session_factory() as s:
            lock_application(s, acme_id)  # first statement: a write, so SQLite waits
            app_row = s.get(Application, acme_id)
            delete_domain(s, app_row, target_id)
            s.commit()
        outcome["result"] = instance_b.run_once()

    thread_b = threading.Thread(target=delete_then_reconcile)
    thread_b.start()
    time.sleep(0.5)
    assert "result" not in outcome, "instance B ran while A still held the lock"

    gate.set()
    thread_a.join(30)
    thread_b.join(30)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    assert outcome["result"].ok
    assert "a.customer.example" not in hostnames_in(caddy.running)
    # A's stale snapshot was applied first and then superseded, never last.
    assert "a.customer.example" in hostnames_in({"apps": caddy.loads[-2]})
    assert "a.customer.example" not in hostnames_in({"apps": caddy.loads[-1]})
    assert instance_a.holder and instance_a.holder == instance_b.holder  # same process
