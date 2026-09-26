import json

import pytest

from app import cli
from app.caddy import saas_template


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    return tmp_path


def _legacy_file(tmp_path, *hostnames):
    config = saas_template.https_template()
    for hostname in hostnames:
        config = saas_template.add_https_domain(hostname, "app.acme.example:443", template=config)
    path = tmp_path / "caddy.json"
    path.write_text(json.dumps(config))
    return str(path)


def _reference_file(tmp_path, mapping):
    path = tmp_path / "refs.json"
    path.write_text(json.dumps(mapping))
    return str(path)


def _run(*args):
    return cli.main(list(args))


def test_cli_applications_and_credentials(cli_env, capsys):
    create = [
        "application",
        "create",
        "--slug",
        "acme",
        "--name",
        "Acme",
        "--cname-target",
        "acme.edge.example.net",
    ]
    assert _run(*create) == 0
    assert _run(*create) == 2
    assert _run("credential", "issue", "--application", "acme", "--label", "ci") == 0
    secret = capsys.readouterr().out.strip().splitlines()[-1]
    assert secret.startswith("cd_")

    assert _run("origin", "register", "--application", "acme", "--host", "app.acme.example") == 0
    assert _run("credential", "list", "--application", "acme") == 0
    listing = capsys.readouterr().out
    assert "ci" in listing and secret not in listing
    assert _run("domain", "purge-tombstones") == 0
    assert _run() == 1


def test_cli_legacy_import_requires_reference_map_or_explicit_mode(cli_env, capsys):
    _run(
        "application",
        "create",
        "--slug",
        "acme",
        "--name",
        "Acme",
        "--cname-target",
        "acme.edge.example.net",
    )
    legacy = _legacy_file(cli_env, "forms.customer.example")
    assert _run("legacy", "import", "--application", "acme", "--file", legacy) == 2
    assert "--reference-map is required" in capsys.readouterr().err

    assert (
        _run(
            "legacy", "import", "--application", "acme", "--file", legacy, "--hostname-as-reference"
        )
        == 0
    )
    assert (
        "imported\tforms.customer.example\tpending_dns\tforms.customer.example"
        in capsys.readouterr().out
    )


def test_cli_legacy_import_is_all_or_nothing_unless_acknowledged(cli_env, capsys):
    _run(
        "application",
        "create",
        "--slug",
        "acme",
        "--name",
        "Acme",
        "--cname-target",
        "acme.edge.example.net",
    )
    legacy = _legacy_file(
        cli_env, "forms.customer.example", "apex.example", "other.customer.example"
    )
    refs = _reference_file(cli_env, {"forms.customer.example": "ws_1"})
    base = [
        "legacy",
        "import",
        "--application",
        "acme",
        "--file",
        legacy,
        "--reference-map",
        refs,
        "--grandfather",
    ]

    # Dry run reports the problems and writes nothing; nonzero because of skips.
    assert _run(*base, "--dry-run") == cli.EXIT_INCOMPLETE_IMPORT
    captured = capsys.readouterr()
    assert "would-import\tforms.customer.example\tprovisioning\tws_1" in captured.out
    assert "nothing written" in captured.out
    assert "skipped\tapex.example\tapex_not_supported" in captured.err
    assert "skipped\tother.customer.example\tmissing_reference" in captured.err

    # Real run with skips and no acknowledgement: rolled back, nonzero.
    assert _run(*base) == cli.EXIT_INCOMPLETE_IMPORT
    captured = capsys.readouterr()
    assert "nothing written" in captured.err
    assert "--allow-skipped" in captured.err

    from app.db.session import get_session_factory
    from app.models import Domain

    with get_session_factory()() as session:
        assert session.query(Domain).count() == 0

    # Acknowledged partial import commits the importable rows.
    assert _run(*base, "--allow-skipped") == 0
    captured = capsys.readouterr()
    assert "imported\tforms.customer.example\tprovisioning\tws_1" in captured.out
    assert "done: 1 to import, 0 existing, 2 skipped" in captured.out
    with get_session_factory()() as session:
        assert [d.hostname for d in session.query(Domain).all()] == ["forms.customer.example"]

    # Re-running after fixing the map is a no-op for existing rows.
    refs = _reference_file(
        cli_env, {"forms.customer.example": "ws_1", "other.customer.example": "ws_2"}
    )
    assert (
        _run(
            "legacy",
            "import",
            "--application",
            "acme",
            "--file",
            legacy,
            "--reference-map",
            refs,
            "--allow-skipped",
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "existing\tforms.customer.example" in captured.out
    assert "imported\tother.customer.example\tpending_dns\tws_2" in captured.out


def test_cli_edge_config_and_dry_run(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("ENABLE_LEGACY_API", "false")
    monkeypatch.setenv("EDGE_ASSERTION_KEYS", "1:" + "k" * 32)
    monkeypatch.setenv("CADDY_STORAGE", "redis")
    monkeypatch.setenv("CADDY_REDIS_ADDRESS", "redis:6379")
    monkeypatch.setenv("CADDY_REDIS_PASSWORD", "topsecret")
    assert _run("edge", "config") == 0
    out = capsys.readouterr().out
    assert '"module": "redis"' in out and "topsecret" not in out and '"***"' in out

    assert _run("edge", "reconcile", "--dry-run") == 0
    assert "0 route(s), 0 hostname(s)" in capsys.readouterr().out

    monkeypatch.setenv("CADDY_ADMIN_URL", "http://127.0.0.1:1")
    assert _run("edge", "reconcile") == 3
    assert "caddy_unavailable" in capsys.readouterr().err

    monkeypatch.setenv("EDGE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")
    assert _run("edge", "config") == 2
    assert "cannot both be true" in capsys.readouterr().err


def test_cli_bootstrap_and_libpq_url(cli_env, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CADDY_STORAGE", "redis")
    monkeypatch.setenv("CADDY_REDIS_ADDRESS", "redis:6379")
    monkeypatch.setenv("CADDY_REDIS_PASSWORD", "topsecret")
    assert _run("edge", "bootstrap") == 0
    out = capsys.readouterr().out
    assert '"admin"' in out and "topsecret" not in out

    target = tmp_path / "bootstrap.json"
    assert _run("edge", "bootstrap", "--output", str(target)) == 0
    document = json.loads(target.read_text())
    assert document["storage"]["password"] == "topsecret"
    assert document["admin"] == {"listen": "localhost:2019"}
    assert [r["@id"] for r in document["apps"]["http"]["servers"]["edge"]["routes"]] == [
        "edge-health",
        "edge-unmatched",
    ]
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    capsys.readouterr()

    assert _run("db", "libpq-url") == 2  # SQLite in this test
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/cd")
    assert _run("db", "libpq-url") == 0
    assert capsys.readouterr().out.strip() == "postgresql://u:p@db:5432/cd"


def test_cli_worker_once_and_workspace_probe_flag(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")  # reconciler off; checks only
    _run(
        "application",
        "create",
        "--slug",
        "acme",
        "--name",
        "Acme",
        "--cname-target",
        "acme.edge.example.net",
    )
    assert _run("application", "set-workspace-probe", "--application", "acme", "--disabled") == 0
    assert "not required" in capsys.readouterr().out
    assert _run("application", "set-workspace-probe", "--application", "acme", "--enabled") == 0
    assert "probe required" in capsys.readouterr().out
    assert _run("worker", "run", "--once") == 0
    assert "checks: 0 processed" in capsys.readouterr().out


def test_cli_origin_verify_activate_and_credential_rotate(cli_env, capsys, monkeypatch):
    from tests.test_origin_verification import TokenServer

    server = TokenServer()
    try:
        _run(
            "application",
            "create",
            "--slug",
            "acme",
            "--name",
            "Acme",
            "--cname-target",
            "acme.edge.example.net",
        )
        assert (
            _run(
                "origin",
                "register",
                "--application",
                "acme",
                "--host",
                "localhost",
                "--scheme",
                "http",
                "--port",
                str(server.port),
            )
            == 0
        )
        out = capsys.readouterr().out
        token = [line for line in out.splitlines() if line.startswith("verification token:")][
            0
        ].split()[-1]
        assert "origin verify" in out

        # Blocked by default: localhost is not public.
        assert _run("origin", "verify", "--application", "acme", "--host", "localhost") == 3
        err = capsys.readouterr().err
        assert "private_address_blocked" in err and "expected: GET" in err

        server.body = b"wrong"
        monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")
        assert _run("origin", "verify", "--application", "acme", "--host", "localhost") == 3
        assert "token_mismatch" in capsys.readouterr().err
        assert _run("origin", "list", "--application", "acme") == 0
        assert "failed\tinactive" in capsys.readouterr().out and True

        server.body = token.encode()
        assert (
            _run("origin", "verify", "--application", "acme", "--host", "localhost", "--activate")
            == 0
        )
        assert "verified and active" in capsys.readouterr().out
        assert _run("origin", "list", "--application", "acme") == 0
        assert "verified\tactive" in capsys.readouterr().out
    finally:
        server.close()

    assert _run("credential", "issue", "--application", "acme", "--label", "backend") == 0
    lines = capsys.readouterr().out.splitlines()
    credential_id = lines[0].split()[-1]
    assert (
        _run(
            "credential",
            "rotate",
            "--application",
            "acme",
            "--id",
            credential_id,
            "--grace-hours",
            "2",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "new credential id" in out and "expires at" in out
    assert _run("credential", "list", "--application", "acme") == 0
    assert capsys.readouterr().out.count("backend") == 2


def test_cli_dev_demo_onboards_the_sample_application(cli_env, capsys, monkeypatch, tmp_path):
    """The demo creates the application, verifies the origin for real and registers domains."""
    import threading

    from tests.test_origin_verification import TokenServer

    env_file = tmp_path / "sample.env"
    server = TokenServer()

    # The origin serves whatever token the demo wrote for it, like the sample container.
    def serve_written_token():
        import time

        for _ in range(100):
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if line.startswith("ORIGIN_VERIFICATION_TOKEN="):
                        server.body = line.split("=", 1)[1].encode()
                return
            time.sleep(0.1)

    threading.Thread(target=serve_written_token, daemon=True).start()
    monkeypatch.setenv("ENABLE_LEGACY_API", "true")  # reconciler off; no Caddy here
    monkeypatch.setenv("DISABLE_HTTPS", "true")
    monkeypatch.setenv("DNS_VERIFICATION_MODE", "local")
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("EDGE_ASSERTION_KEYS", "1:local-development-only-key-0000000000000000")
    try:
        code = _run(
            "dev",
            "demo",
            "--origin-host",
            "localhost",
            "--origin-port",
            str(server.port),
            "--write-env",
            str(env_file),
            "--wait",
            "10",
        )
    finally:
        server.close()
    out = capsys.readouterr().out
    assert code == 3, out  # no worker ran, so the domains are registered but not ready
    assert "created application sample" in out and "verified and active" in out
    assert "registered alpha.sample.localtest.me for workspace ws_alpha" in out
    assert "cd_" in out and "http://alpha.sample.localtest.me/" in out
    written = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert set(written) == {"APPLICATION_ID", "EDGE_ASSERTION_KEYS", "ORIGIN_VERIFICATION_TOKEN"}

    # Running it again reuses the application, origin and domains.
    assert (
        _run(
            "dev",
            "demo",
            "--origin-host",
            "localhost",
            "--origin-port",
            str(server.port),
            "--wait",
            "0",
        )
        == 0
    )
    again = capsys.readouterr().out
    assert "application sample exists" in again and "registered alpha" not in again

    # The worker, with the local resolver, takes the domains to provisioning
    # (no edge here, so not further).
    monkeypatch.setenv("EDGE_RECONCILE_ENABLED", "false")
    assert _run("checks", "run") == 0
    assert _run("application", "list") == 0
    capsys.readouterr()
    from app.models import DomainStatus
    from app.services import applications as app_service
    from app.services.domains import find_live_by_hostname

    with cli.get_session_factory()() as s:
        acme = app_service.get_application_by_slug(s, "sample")
        alpha = find_live_by_hostname(s, "alpha.sample.localtest.me")
        assert alpha.application_id == acme.id
        assert alpha.status == DomainStatus.PROVISIONING


def test_cli_doctor_reports_the_deployment_state(cli_env, capsys, monkeypatch):
    """Offline: the network checks are answered by fakes through the service layer."""
    from app.dns.settings import DnsSettings
    from app.edge.probe import EdgeProbeFailed
    from app.edge.settings import EdgeSettings
    from app.services import doctor as doctor_module
    from app.services.applications import (
        activate_origin,
        create_application,
        record_origin_verification,
        register_origin,
    )

    reached: list[str] = []

    def fake_http_get(url):
        reached.append(url)
        if url.endswith("/config/apps") or url == doctor_module.ACME_DIRECTORY:
            return 200, {}
        raise OSError("unreachable")

    from app.edge.config import build_apps

    def fake_fetch_json(url):
        # The edge runs exactly what this instance derives.
        with factory() as s:
            return build_apps(s, settings)

    def fake_resolve(host, dns_settings):
        # Globally routable example addresses (example.com's), never documentation ranges.
        return {
            "edge.acme.example": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
            "edge.globex.example": ["93.184.215.14"],
            "edge.self.example": ["127.0.1.1"],  # the host's own name, seen from a container
            "edge.cgnat.example": ["100.64.0.1"],  # shared address space (RFC 6598)
        }.get(host) or (_ for _ in ()).throw(LookupError(f"{host} does not exist in public DNS"))

    def fake_probe(hostname, address, settings):
        if hostname == "edge.acme.example":
            return 204, "1"
        return 308, None  # another web server: a redirect, no marker

    settings = EdgeSettings(
        reconcile_enabled=True,
        legacy_api_enabled=False,
        edge_token="x" * 40,
        assertion_keys=(("1", "k" * 40),),
    )
    factory = cli.get_session_factory()

    # Fresh database: nothing to report but the missing application and reconciler.
    findings = doctor_module.run_doctor(
        factory,
        settings,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve,
        probe_target=fake_probe,
        fetch_json=fake_fetch_json,
    )
    by_check = {f.check: f for f in findings}
    assert by_check["database"].ok and by_check["migrations"].ok
    assert by_check["edge gateway"].ok and by_check["acme"].ok and by_check["edge token"].ok
    assert by_check["edge config"].ok
    assert by_check["reconciler"].status == "warn"
    assert by_check["applications"].status == "warn"
    assert by_check["edge hostname"].status == "warn"  # EDGE_HOSTNAME not set

    # With the edge's own name set, it is checked before any application exists.
    named = EdgeSettings(**{**settings.__dict__, "edge_hostname": "edge.acme.example"})
    findings = doctor_module.run_doctor(
        factory,
        named,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve,
        probe_target=fake_probe,
    )
    by_check = {f.check: f for f in findings}
    assert by_check["edge hostname edge.acme.example"].ok

    # The edge running something else (the gateway rejecting the reconciler, as
    # when EDGE_ASK_URL differs between containers) is a failure that says so.
    def stale_fetch_json(url):
        return {
            "http": {
                "servers": {"edge": {"routes": [{"@id": "edge-health"}, {"@id": "edge-unmatched"}]}}
            }
        }

    findings = doctor_module.run_doctor(
        factory,
        named,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve,
        probe_target=fake_probe,
        fetch_json=stale_fetch_json,
    )
    by_check = {f.check: f for f in findings}
    stale = by_check["edge config"]
    assert (
        stale.status == "fail"
        and "config_rejected" in stale.detail
        and "EDGE_ASK_URL" in stale.detail
    )
    assert "without TLS automation" in stale.detail

    # An address family this container cannot route to (IPv6 inside Docker) is
    # reported as unverifiable, not as a failure of the edge.
    def probe_no_v6(hostname, address, settings):
        if ":" in address:
            raise EdgeProbeFailed(
                "connection_failed",
                f"Cannot connect to {address}: [Errno 101] Network is unreachable",
            )
        return 204, "1"

    findings = doctor_module.run_doctor(
        factory,
        named,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve,
        probe_target=probe_no_v6,
    )
    by_check = {f.check: f for f in findings}
    hostname_check = by_check["edge hostname edge.acme.example"]
    assert hostname_check.status == "warn" and "could not be checked" in hostname_check.detail
    assert "curl -6" in hostname_check.detail

    # But an IPv4 failure alongside an unroutable IPv6 is still a failure, and
    # the unroutable IPv6 is named as not checkable rather than blamed.
    def probe_v4_broken(hostname, address, settings):
        if ":" in address:
            raise EdgeProbeFailed("connection_failed", "[Errno 101] Network is unreachable")
        return 404, None

    findings = doctor_module.run_doctor(
        factory,
        named,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve,
        probe_target=probe_v4_broken,
    )
    by_check = {f.check: f for f in findings}
    mixed = by_check["edge hostname edge.acme.example"]
    assert mixed.status == "fail" and "not checkable from this container" in mixed.detail

    # An unroutable IPv4 address is a genuine failure: only IPv6 gets the
    # inside-Docker allowance, so an A-only target with no route fails.
    def probe_v4_unroutable(hostname, address, settings):
        raise EdgeProbeFailed(
            "connection_failed", f"Cannot connect to {address}: [Errno 101] Network is unreachable"
        )

    def resolve_v4_only(host, dns_settings):
        return ["93.184.216.34"]

    findings = doctor_module.run_doctor(
        factory,
        named,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=resolve_v4_only,
        probe_target=probe_v4_unroutable,
    )
    by_check = {f.check: f for f in findings}
    v4_only = by_check["edge hostname edge.acme.example"]
    assert v4_only.status == "fail" and "Network is unreachable" in v4_only.detail
    assert "could not be checked" not in v4_only.detail and "curl -6" not in v4_only.detail

    with factory() as s:
        acme = create_application(s, slug="acme", name="Acme", cname_target="edge.acme.example")
        origin = register_origin(s, acme, host="app.acme.example")
        record_origin_verification(s, origin, verified=True)
        activate_origin(s, origin)
        create_application(s, slug="globex", name="Globex", cname_target="edge.globex.example")
        create_application(s, slug="nowhere", name="Nowhere", cname_target="edge.nowhere.example")
        create_application(s, slug="selfie", name="Self", cname_target="edge.self.example")
        create_application(s, slug="cgnat", name="CGNAT", cname_target="edge.cgnat.example")
        s.commit()
    findings = doctor_module.run_doctor(
        factory,
        settings,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve,
        probe_target=fake_probe,
    )
    by_check = {f.check: f for f in findings}
    assert by_check["application acme"].ok
    assert by_check["cname target edge.acme.example"].ok  # both address families answer
    assert by_check["application globex"].status == "warn"  # no origin
    # A plain redirect is what any web server does; only the edge's marker passes.
    assert by_check["cname target edge.globex.example"].status == "fail"
    assert "without this edge's marker" in by_check["cname target edge.globex.example"].detail
    assert by_check["cname target edge.nowhere.example"].status == "fail"
    # A loopback answer (the machine's own hostname shadowing the record) is
    # named as such rather than probed.
    assert by_check["cname target edge.self.example"].status == "fail"
    assert "not a public address" in by_check["cname target edge.self.example"].detail
    # Shared address space is not routable from the Internet either.
    assert by_check["cname target edge.cgnat.example"].status == "fail"
    assert "not a public address" in by_check["cname target edge.cgnat.example"].detail
    assert doctor_module.summarize(findings)[2] == 4

    # A former target still named by a live claim stays monitored: the new
    # target is healthy, the old one is broken, and doctor says so.
    from app.services.applications import get_application_by_slug, set_cname_target
    from app.services.domains import claim_domain

    with factory() as s:
        acme = get_application_by_slug(s, "acme")
        claim_domain(s, acme, "old.customer.example", "w-old")
        set_cname_target(s, acme, "edge2.acme.example")
        claim_domain(s, acme, "new.customer.example", "w-new")
        s.commit()

    def fake_resolve_moved(host, dns_settings):
        if host == "edge2.acme.example":
            return ["93.184.216.35"]
        if host == "edge.acme.example":
            raise LookupError("edge.acme.example does not exist in public DNS")
        return fake_resolve(host, dns_settings)

    def fake_probe_moved(hostname, address, settings):
        return (204, "1") if hostname == "edge2.acme.example" else (308, None)

    findings = doctor_module.run_doctor(
        factory,
        settings,
        DnsSettings(),
        http_get=fake_http_get,
        resolve=fake_resolve_moved,
        probe_target=fake_probe_moved,
    )
    by_check = {f.check: f for f in findings}
    assert by_check["cname target edge2.acme.example"].ok
    former = by_check["cname target edge.acme.example (former, still named by live claims)"]
    assert former.status == "fail" and "does not exist" in former.detail

    # The command prints the table and fails when a check fails; with the
    # edge gateway unreachable (no edge here) it reports that as a failure.
    monkeypatch.setenv("ENABLE_LEGACY_API", "false")
    monkeypatch.setenv("EDGE_ASSERTION_KEYS", "1:" + "k" * 40)
    monkeypatch.setenv("CADDY_ADMIN_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        doctor_module, "_http_get", lambda url: (_ for _ in ()).throw(OSError("down"))
    )
    monkeypatch.setattr(doctor_module, "_resolve", fake_resolve)
    monkeypatch.setattr(doctor_module, "_probe_target", fake_probe)
    assert _run("doctor") == 1
    out = capsys.readouterr().out
    assert "FAIL  edge gateway" in out and "failure(s)" in out


def test_cli_application_set_cname_target(cli_env, capsys):
    from app.models import DomainStatus
    from app.services import applications as app_service
    from app.services.domains import claim_domain, find_live_by_hostname

    _run(
        "application",
        "create",
        "--slug",
        "acme",
        "--name",
        "Acme",
        "--cname-target",
        "old.edge.example",
    )
    with cli.get_session_factory()() as s:
        acme = app_service.get_application_by_slug(s, "acme")
        claim_domain(s, acme, "a.customer.example", "w1")
        s.commit()
    capsys.readouterr()

    # Change without touching existing domains: new claims use the new name,
    # the existing one keeps what its customer published.
    assert (
        _run(
            "application",
            "set-cname-target",
            "--application",
            "acme",
            "--cname-target",
            "New.Edge.Example.",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "old.edge.example -> new.edge.example" in out and "1 live domain(s) still" in out
    with cli.get_session_factory()() as s:
        acme = app_service.get_application_by_slug(s, "acme")
        assert acme.cname_target == "new.edge.example"
        assert (
            find_live_by_hostname(s, "a.customer.example").active_claim.cname_target
            == "old.edge.example"
        )
        b = claim_domain(s, acme, "b.customer.example", "w2")
        assert b.active_claim.cname_target == "new.edge.example"
        s.commit()

    # Re-issuing moves the stale domain to the new target and resets it.
    assert (
        _run(
            "application",
            "set-cname-target",
            "--application",
            "acme",
            "--cname-target",
            "new.edge.example",
            "--reissue-claims",
        )
        == 0
    )
    assert "re-issued the claim of 1 domain(s)" in capsys.readouterr().out
    with cli.get_session_factory()() as s:
        a = find_live_by_hostname(s, "a.customer.example")
        assert a.active_claim.cname_target == "new.edge.example"
        assert a.status == DomainStatus.PENDING_DNS

    assert (
        _run("application", "set-cname-target", "--application", "acme", "--cname-target", "*.bad")
        != 0
    )


def test_cli_application_create_defaults_the_cname_target_to_the_edge_hostname(
    cli_env, capsys, monkeypatch
):
    monkeypatch.delenv("EDGE_HOSTNAME", raising=False)
    assert _run("application", "create", "--slug", "noedge", "--name", "No Edge") == 2
    assert "EDGE_HOSTNAME" in capsys.readouterr().err
    monkeypatch.setenv("EDGE_HOSTNAME", "edge.example.net")
    assert _run("application", "create", "--slug", "acme", "--name", "Acme") == 0
    capsys.readouterr()
    assert _run("application", "list") == 0
    assert "edge.example.net" in capsys.readouterr().out
