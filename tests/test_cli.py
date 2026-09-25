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


def test_cli_end_to_end(cli_env, capsys):
    assert (
        cli.main(
            [
                "application",
                "create",
                "--slug",
                "acme",
                "--name",
                "Acme",
                "--cname-target",
                "acme.edge.example.net",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "application",
                "create",
                "--slug",
                "acme",
                "--name",
                "Acme",
                "--cname-target",
                "acme.edge.example.net",
            ]
        )
        == 2
    )
    assert cli.main(["credential", "issue", "--application", "acme", "--label", "ci"]) == 0
    out = capsys.readouterr().out
    secret = out.strip().splitlines()[-1]
    assert secret.startswith("cd_")

    assert (
        cli.main(["origin", "register", "--application", "acme", "--host", "app.acme.example"]) == 0
    )

    config = saas_template.add_https_domain(
        "forms.customer.example", "app.acme.example:443", template=saas_template.https_template()
    )
    legacy_file = cli_env / "caddy.json"
    legacy_file.write_text(json.dumps(config))
    assert (
        cli.main(
            ["legacy", "import", "--application", "acme", "--file", str(legacy_file), "--dry-run"]
        )
        == 0
    )
    assert "nothing written" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "legacy",
                "import",
                "--application",
                "acme",
                "--file",
                str(legacy_file),
                "--grandfather",
            ]
        )
        == 0
    )
    assert "imported\tforms.customer.example\tprovisioning" in capsys.readouterr().out

    assert cli.main(["credential", "list", "--application", "acme"]) == 0
    listing = capsys.readouterr().out
    assert "ci" in listing and secret not in listing
    assert cli.main(["domain", "purge-tombstones"]) == 0
    assert cli.main([]) == 1
