"""deploy/install.sh: fresh install and upgrade, run hermetically with a stub docker."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "install.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("openssl") is None,
    reason="bash and openssl are required",
)


def run_install(
    tmp_path: Path, version: str | None = None, **env: str
) -> subprocess.CompletedProcess:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    if not docker.exists():
        # Records every invocation; `compose version` succeeds so the script continues.
        docker.write_text(
            f'#!/usr/bin/env bash\necho "docker $*" >> "{tmp_path}/docker.log"\nexit 0\n'
        )
        docker.chmod(0o755)
    environment = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CUSTOM_DOMAIN_DIR": str(tmp_path / "opt"),
        "CUSTOM_DOMAIN_BIN_DIR": str(tmp_path / "bin"),
        "CUSTOM_DOMAIN_SOURCE": str(ROOT),
        "CUSTOM_DOMAIN_SKIP_ROOT_CHECK": "1",
        "SKIP_DOCKER_INSTALL": "1",
        "SKIP_FIREWALL": "1",
        **env,
    }
    if version is not None:
        environment["CUSTOM_DOMAIN_VERSION"] = version
    return subprocess.run(
        ["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, timeout=120
    )


def env_of(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "opt" / "deploy" / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_fresh_install_writes_everything_and_starts_the_stack(tmp_path):
    result = run_install(
        tmp_path,
        "0.9.9",
        ACME_EMAIL="ops@example.net",
        EDGE_HOSTNAME="Edge.Example.Net",
        PORTAL_ALLOWED_IPS="203.0.113.9",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    deploy = tmp_path / "opt" / "deploy"
    assert (deploy / "compose.production.yml").read_text() == (
        ROOT / "deploy" / "compose.production.yml"
    ).read_text()
    assert (deploy / ".compose.production.yml.installed").exists()
    env = env_of(tmp_path)
    assert env["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.9"
    assert env["EDGE_HOSTNAME"] == "Edge.Example.Net"  # canonicalized by the service on load
    assert env["PORTAL_ALLOWED_IPS"] == "203.0.113.9" and env["ACME_EMAIL"] == "ops@example.net"
    for key in ("POSTGRES_PASSWORD", "EDGE_TOKEN", "PORTAL_PASSWORD", "CADDY_REDIS_PASSWORD"):
        assert len(env[key]) >= 32
    assert env["EDGE_ASSERTION_KEYS"].startswith("1:") and len(env["EDGE_ASSERTION_KEYS"]) > 40
    assert oct((deploy / ".env").stat().st_mode & 0o777) == "0o600"
    wrapper = (tmp_path / "bin" / "custom-domain").read_text()
    assert "exec api custom-domain" in wrapper and '"upgrade"' in wrapper
    log = (tmp_path / "docker.log").read_text()
    assert "pull -q" in log and "up -d --wait" in log
    assert "https://edge.example.net/portal" in result.stdout.lower()
    assert "127.0.0.1:9000" in result.stdout


def test_upgrade_refreshes_compose_adds_settings_and_moves_the_image(tmp_path):
    first = run_install(tmp_path, "0.9.8")
    assert first.returncode == 0, first.stderr
    deploy = tmp_path / "opt" / "deploy"
    # Simulate an installation made by an older release: an older Compose file
    # (recorded as installed) and an .env without the newer settings.
    old_compose = "services:\n  api:\n    image: old\n"
    (deploy / "compose.production.yml").write_text(old_compose)
    (deploy / ".compose.production.yml.installed").write_text(
        subprocess.run(
            ["sha256sum", str(deploy / "compose.production.yml")], capture_output=True, text=True
        ).stdout.split()[0]
        + "\n"
    )
    env_text = (deploy / ".env").read_text()
    env_text = "\n".join(
        line for line in env_text.splitlines() if not line.startswith(("PORTAL_", "EDGE_HOSTNAME="))
    )
    (deploy / ".env").write_text(env_text + "\n")
    secrets_before = {
        k: v for k, v in env_of(tmp_path).items() if k in ("POSTGRES_PASSWORD", "EDGE_TOKEN")
    }

    # Re-run without a version: the Compose file is refreshed (it was unmodified),
    # the missing settings are added, the image is kept.
    again = run_install(tmp_path, None, EDGE_HOSTNAME="edge.example.net")
    assert again.returncode == 0, again.stderr + again.stdout
    assert (deploy / "compose.production.yml").read_text() == (
        ROOT / "deploy" / "compose.production.yml"
    ).read_text()
    env = env_of(tmp_path)
    assert env["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.8"
    assert len(env["PORTAL_PASSWORD"]) >= 32 and env["EDGE_HOSTNAME"] == "edge.example.net"
    assert "PORTAL_ALLOWED_IPS" in env
    assert {k: env[k] for k in secrets_before} == secrets_before  # secrets untouched
    assert "Keeping image" in again.stdout

    # An explicit version moves the image.
    moved = run_install(tmp_path, "0.9.9")
    assert moved.returncode == 0, moved.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.9"


def test_upgrade_keeps_a_locally_modified_compose_file(tmp_path):
    assert run_install(tmp_path, "0.9.8").returncode == 0
    deploy = tmp_path / "opt" / "deploy"
    customized = (deploy / "compose.production.yml").read_text() + "# operator change\n"
    (deploy / "compose.production.yml").write_text(customized)
    result = run_install(tmp_path, "0.9.9")
    assert result.returncode == 0, result.stderr
    assert (deploy / "compose.production.yml").read_text() == customized
    assert (deploy / "compose.production.yml.new").read_text() == (
        ROOT / "deploy" / "compose.production.yml"
    ).read_text()
    assert "modified locally" in result.stderr
