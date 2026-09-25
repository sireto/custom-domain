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
