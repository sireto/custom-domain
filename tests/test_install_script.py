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


def sha256(path: Path) -> str:
    return subprocess.run(["sha256sum", str(path)], capture_output=True, text=True).stdout.split()[
        0
    ]


def release_with_changed_compose(tmp_path: Path) -> Path:
    """A fake newer release whose Compose file differs from the repository's."""
    source = tmp_path / "release"
    (source / "deploy").mkdir(parents=True, exist_ok=True)
    (source / "deploy" / "compose.production.yml").write_text(
        (ROOT / "deploy" / "compose.production.yml").read_text()
        + "# upstream change in a later release\n"
    )
    return source


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
    assert (deploy / ".compose.production.yml.upstream").exists()
    env = env_of(tmp_path)
    assert env["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.9"
    assert env["EDGE_HOSTNAME"] == "Edge.Example.Net"  # canonicalized by the service on load
    assert env["PORTAL_ALLOWED_IPS"] == "203.0.113.9" and env["ACME_EMAIL"] == "ops@example.net"
    assert env["PUBLIC_API"] == "true"  # new installs serve the API through the edge
    assert "https://Edge.Example.Net/v1" in result.stdout
    for key in (
        "POSTGRES_PASSWORD",
        "EDGE_TOKEN",
        "PORTAL_PASSWORD",
        "CADDY_REDIS_PASSWORD",
        "SERVICE_SECRET_KEY",
    ):
        assert len(env[key]) >= 32
    assert env["EDGE_ASSERTION_KEYS"].startswith("1:") and len(env["EDGE_ASSERTION_KEYS"]) > 40
    assert oct((deploy / ".env").stat().st_mode & 0o777) == "0o600"
    wrapper = (tmp_path / "bin" / "custom-domain").read_text()
    assert "exec api custom-domain" in wrapper and '"upgrade"' in wrapper
    assert "--accept-compose" in wrapper and "CUSTOM_DOMAIN_ACCEPT_COMPOSE" in wrapper
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
    old_sum = sha256(deploy / "compose.production.yml")
    (deploy / ".compose.production.yml.installed").write_text(old_sum + "\n")
    (deploy / ".compose.production.yml.upstream").write_text(old_sum + "\n")
    env_text = (deploy / ".env").read_text()
    env_text = "\n".join(
        line
        for line in env_text.splitlines()
        if not line.startswith(("PORTAL_", "EDGE_HOSTNAME=", "SERVICE_SECRET_KEY="))
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
    assert len(env["SERVICE_SECRET_KEY"]) >= 32  # added for releases that predate it
    assert {k: env[k] for k in secrets_before} == secrets_before  # secrets untouched
    assert "Keeping image" in again.stdout

    # An explicit version moves the image.
    moved = run_install(tmp_path, "0.9.9")
    assert moved.returncode == 0, moved.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.9"


def test_upgrade_stops_on_a_locally_modified_compose_file_until_accepted(tmp_path):
    assert run_install(tmp_path, "0.9.8").returncode == 0
    deploy = tmp_path / "opt" / "deploy"
    (tmp_path / "docker.log").unlink()
    customized = (deploy / "compose.production.yml").read_text() + "# operator change\n"
    (deploy / "compose.production.yml").write_text(customized)

    # Nothing changes: the image stays, docker is not called, the exit code says why.
    result = run_install(tmp_path, "0.9.9")
    assert result.returncode == 3
    assert (deploy / "compose.production.yml").read_text() == customized
    assert (deploy / "compose.production.yml.new").read_text() == (
        ROOT / "deploy" / "compose.production.yml"
    ).read_text()
    assert "Nothing was changed" in result.stderr and "--accept-compose" in result.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.8"
    calls = (tmp_path / "docker.log").read_text() if (tmp_path / "docker.log").exists() else ""
    assert "pull" not in calls and "up -d" not in calls  # only the `compose version` probe

    # The operator merges (here: keeps their change on top of the release's file)
    # and accepts: the merged file is recorded as installed and the upgrade runs.
    merged = (ROOT / "deploy" / "compose.production.yml").read_text() + "# operator change\n"
    (deploy / "compose.production.yml").write_text(merged)
    accepted = run_install(tmp_path, "0.9.9", CUSTOM_DOMAIN_ACCEPT_COMPOSE="1")
    assert accepted.returncode == 0, accepted.stderr
    assert (deploy / "compose.production.yml").read_text() == merged
    assert not (deploy / "compose.production.yml.new").exists()
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.9"
    assert "up -d --wait" in (tmp_path / "docker.log").read_text()

    # The accepted file is the baseline: a same-version rerun and a later release
    # that does not change the Compose file both keep the customization.
    assert run_install(tmp_path, "0.9.9").returncode == 0
    assert (deploy / "compose.production.yml").read_text() == merged
    assert run_install(tmp_path, "0.9.10").returncode == 0
    assert (deploy / "compose.production.yml").read_text() == merged
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.10"

    # A later release that changes the upstream file stops for another merge
    # instead of overwriting the customization.
    later = release_with_changed_compose(tmp_path)
    stopped = run_install(tmp_path, "0.9.11", CUSTOM_DOMAIN_SOURCE=str(later))
    assert stopped.returncode == 3 and "changes the upstream file" in stopped.stderr
    assert (deploy / "compose.production.yml").read_text() == merged
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.10"
    assert (deploy / "compose.production.yml.new").read_text() == (
        later / "deploy" / "compose.production.yml"
    ).read_text()
    merged_again = (later / "deploy" / "compose.production.yml").read_text() + "# operator change\n"
    (deploy / "compose.production.yml").write_text(merged_again)
    accepted_again = run_install(
        tmp_path, "0.9.11", CUSTOM_DOMAIN_SOURCE=str(later), CUSTOM_DOMAIN_ACCEPT_COMPOSE="1"
    )
    assert accepted_again.returncode == 0, accepted_again.stderr
    assert (deploy / "compose.production.yml").read_text() == merged_again
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.11"


def test_installation_that_predates_the_installer_is_treated_as_modified(tmp_path):
    """A hand-copied Compose file with no checksum recorded: stop, do not restart."""
    deploy = tmp_path / "opt" / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "compose.production.yml").write_text("services:\n  api:\n    image: old\n")
    (deploy / ".env").write_text(
        "DATABASE_URL=x\nCUSTOM_DOMAIN_IMAGE=ghcr.io/sireto/custom-domain:0.3.0\n"
    )
    result = run_install(tmp_path, "0.9.9")
    assert result.returncode == 3 and "predates the installer" in result.stderr
    assert (deploy / "compose.production.yml.new").exists()
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.3.0"
    calls = (tmp_path / "docker.log").read_text() if (tmp_path / "docker.log").exists() else ""
    assert "pull" not in calls and "up -d" not in calls


def test_explicit_version_wins_over_the_cloud_init_config_file(tmp_path):
    """cloud-init leaves the original version in /etc/custom-domain-install.env;
    `custom-domain upgrade 0.9.9` must still install 0.9.9."""
    config = tmp_path / "install.env"
    config.write_text(
        "CUSTOM_DOMAIN_VERSION=0.3.1\nACME_EMAIL=from-file@example.net\nEDGE_HOSTNAME=edge.example.net\n"
    )
    first = run_install(tmp_path, None, CUSTOM_DOMAIN_INSTALL_ENV=str(config))
    assert first.returncode == 0, first.stderr
    env = env_of(tmp_path)
    assert env["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.3.1"  # the file's default
    assert env["ACME_EMAIL"] == "from-file@example.net"
    upgraded = run_install(tmp_path, "0.9.9", CUSTOM_DOMAIN_INSTALL_ENV=str(config))
    assert upgraded.returncode == 0, upgraded.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == "ghcr.io/sireto/custom-domain:0.9.9"
    assert "Image set to ghcr.io/sireto/custom-domain:0.9.9" in upgraded.stdout


def test_upgrade_leaves_public_api_alone_unless_given(tmp_path):
    assert run_install(tmp_path, "0.9.8").returncode == 0
    deploy = tmp_path / "opt" / "deploy"
    # An installation from before PUBLIC_API existed.
    text = (deploy / ".env").read_text()
    kept = [line for line in text.splitlines() if not line.startswith("PUBLIC_API=")]
    (deploy / ".env").write_text("\n".join(kept) + "\n")
    assert run_install(tmp_path, None).returncode == 0
    assert "PUBLIC_API" not in env_of(tmp_path)  # not turned on behind the operator's back
    assert run_install(tmp_path, None, PUBLIC_API="true").returncode == 0
    assert env_of(tmp_path)["PUBLIC_API"] == "true"
    assert run_install(tmp_path, None, PUBLIC_API="false").returncode == 0
    assert env_of(tmp_path)["PUBLIC_API"] == "false"


def test_summary_shows_the_public_address_behind_nat_and_one_cname_hint(tmp_path):
    """On EC2 the server sees only its private address; the A record needs the public one."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "ip").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *-4*) echo "1.1.1.1 via 10.0.0.1 dev ens5 src 10.0.0.12 uid 0" ;; esac\n'
    )
    (stubs / "curl").write_text(
        '#!/usr/bin/env bash\ncase "$*" in *checkip*) echo "198.51.100.7" ;; *) exit 22 ;; esac\n'
    )
    for stub in ("ip", "curl"):
        (stubs / stub).chmod(0o755)
    result = run_install(tmp_path, "0.9.9", EDGE_HOSTNAME="edge.example.net")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "This server:        198.51.100.7   (private address 10.0.0.12)" in result.stdout
    assert "A 198.51.100.7" in result.stdout and "A 10.0.0.12" not in result.stdout
    create = [line for line in result.stdout.splitlines() if "application create" in line][0]
    assert create.endswith("(CNAME target defaults to edge.example.net)"), create

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "stubs").mkdir()
    for stub in ("ip", "curl"):
        (plain / "stubs" / stub).write_text((stubs / stub).read_text())
        (plain / "stubs" / stub).chmod(0o755)
    without = run_install(plain, "0.9.9")
    assert without.returncode == 0, without.stderr
    create = [line for line in without.stdout.splitlines() if "application create" in line][0]
    assert create.endswith('--name "Acme" --cname-target edge.example.net'), create


def test_upgrade_brings_an_untouched_compose_file_up_to_date(tmp_path):
    """A release that changes the Compose file (0.6.1 adds log rotation) replaces an
    unmodified installed copy without stopping for a merge."""
    release = tmp_path / "old-release"
    (release / "deploy").mkdir(parents=True)
    current = (ROOT / "deploy" / "compose.production.yml").read_text()
    previous = "\n".join(
        line
        for line in current.splitlines()
        if "logging" not in line
        and "max-" not in line
        and "driver: json-file" not in line
        and "options:" not in line
    )
    (release / "deploy" / "compose.production.yml").write_text(previous + "\n")
    first = run_install(tmp_path, "0.9.8", CUSTOM_DOMAIN_SOURCE=str(release))
    assert first.returncode == 0, first.stderr + first.stdout
    upgraded = run_install(tmp_path, "0.9.9")
    assert upgraded.returncode == 0, upgraded.stderr + upgraded.stdout
    installed = (tmp_path / "opt" / "deploy" / "compose.production.yml").read_text()
    assert installed == current and "max-size" in installed


TOKEN = "op-token-0123456789abcdef-0123456789abcdef"


def test_operator_settings_are_written_and_the_token_never_printed(tmp_path):
    result = run_install(
        tmp_path,
        "0.6.1",
        OPERATOR_API_TOKEN=TOKEN,
        OPERATOR_ALLOWED_IPS="198.51.100.7,2001:db8::/48",
        MAX_DOMAINS="2000",
    )
    assert result.returncode == 0, result.stderr
    env = env_of(tmp_path)
    assert env["OPERATOR_API_TOKEN"] == TOKEN
    assert env["OPERATOR_ALLOWED_IPS"] == "198.51.100.7,2001:db8::/48"
    assert env["MAX_DOMAINS"] == "2000"
    assert TOKEN not in result.stdout + result.stderr

    # An upgrade keeps them unless given, and replaces them when given.
    assert run_install(tmp_path, "0.6.1").returncode == 0
    assert env_of(tmp_path)["MAX_DOMAINS"] == "2000"
    assert env_of(tmp_path)["OPERATOR_API_TOKEN"] == TOKEN
    assert run_install(tmp_path, None, MAX_DOMAINS="20000").returncode == 0
    assert env_of(tmp_path)["MAX_DOMAINS"] == "20000"


def test_installs_without_operator_settings_leave_them_out(tmp_path):
    assert run_install(tmp_path, "0.6.1").returncode == 0
    env = env_of(tmp_path)
    assert not {"OPERATOR_API_TOKEN", "OPERATOR_ALLOWED_IPS", "MAX_DOMAINS"} & set(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPERATOR_API_TOKEN", "too-short"),
        ("OPERATOR_API_TOKEN", "a" * 31 + "|x"),
        ("OPERATOR_API_TOKEN", "a" * 32 + " b"),
        ("OPERATOR_ALLOWED_IPS", "198.51.100.7;rm -rf /"),
        ("MAX_DOMAINS", "0"),
        ("MAX_DOMAINS", "lots"),
    ],
)
def test_unusable_operator_settings_stop_before_anything_changes(tmp_path, name, value):
    result = run_install(tmp_path, "0.6.1", **{name: value})
    assert result.returncode == 1 and name in result.stderr
    assert not (tmp_path / "opt").exists()
    assert value not in result.stdout


def test_operator_backup_is_written_only_when_given(tmp_path):
    assert run_install(tmp_path, "0.6.1", OPERATOR_BACKUP="true").returncode == 0
    assert env_of(tmp_path)["OPERATOR_BACKUP"] == "true"
    refused = run_install(tmp_path, None, OPERATOR_BACKUP="yes please")
    assert refused.returncode == 1 and "OPERATOR_BACKUP" in refused.stderr


def test_compose_substitutions_come_from_dotenv_alone(tmp_path):
    """The edge gets its settings through ${...}, api and worker through env_file.
    A value left in the installer's shell (ACME_EMAIL from the install config)
    must not reach the edge alone, or the gateway refuses every configuration."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    docker = stubs / "docker"
    # Records what Compose would substitute from the shell for each invocation.
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "docker $* ACME_EMAIL=${{ACME_EMAIL-unset}} EDGE_TOKEN=${{EDGE_TOKEN-unset}}"'
        f' >> "{tmp_path}/docker.log"\nexit 0\n'
    )
    docker.chmod(0o755)
    config = tmp_path / "install.env"
    config.write_text("ACME_EMAIL=from-file@example.net\n")
    first = run_install(tmp_path, "0.9.8", CUSTOM_DOMAIN_INSTALL_ENV=str(config))
    assert first.returncode == 0, first.stderr
    # The operator changes the address in .env later; an upgrade keeps reading the file.
    upgraded = run_install(
        tmp_path, None, CUSTOM_DOMAIN_INSTALL_ENV=str(config), EDGE_TOKEN="from-shell"
    )
    assert upgraded.returncode == 0, upgraded.stderr
    compose_calls = [
        line
        for line in (tmp_path / "docker.log").read_text().splitlines()
        if " pull " in line or " up " in line
    ]
    assert len(compose_calls) == 4
    for line in compose_calls:
        assert line.endswith("ACME_EMAIL=unset EDGE_TOKEN=unset"), line


def test_installer_clears_every_name_the_compose_file_substitutes():
    import re

    compose = (ROOT / "deploy" / "compose.production.yml").read_text()
    substituted = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", compose))
    script = SCRIPT.read_text()
    match = re.search(r'COMPOSE_SUBSTITUTED="([^"]*)"', script)
    assert match, "install.sh must list the names it clears before running Compose"
    cleared = set(match.group(1).replace("\\\n", " ").split())
    assert substituted == cleared, (
        f"missing: {sorted(substituted - cleared)}, extra: {sorted(cleared - substituted)}"
    )
    assert script.index("unset ${COMPOSE_SUBSTITUTED}") < script.index("${COMPOSE} pull")


DIGEST = "sha256:" + "0123456789abcdef" * 4
OTHER_DIGEST = "sha256:" + "fedcba9876543210" * 4
REPOSITORY = "ghcr.io/sireto/custom-domain"


def test_digest_pins_the_image_by_content(tmp_path):
    result = run_install(tmp_path, "0.9.9", CUSTOM_DOMAIN_IMAGE_DIGEST=DIGEST)
    assert result.returncode == 0, result.stderr + result.stdout
    # name:tag@digest: Compose and `docker pull` fetch by the digest, the tag
    # only says which release it is.
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.9@{DIGEST}"
    assert f"Image:              {REPOSITORY}:0.9.9@{DIGEST}" in result.stdout


def test_digest_pin_follows_the_version_across_upgrades(tmp_path):
    assert run_install(tmp_path, "0.9.8", CUSTOM_DOMAIN_IMAGE_DIGEST=DIGEST).returncode == 0
    pinned = f"{REPOSITORY}:0.9.8@{DIGEST}"

    # A rerun without a version, or with the same version, keeps the pin.
    assert run_install(tmp_path, None).returncode == 0
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == pinned
    same = run_install(tmp_path, "0.9.8")
    assert same.returncode == 0, same.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == pinned
    assert "Keeping the digest pin of 0.9.8" in same.stdout

    # Another version without a digest: the old digest would run the old
    # image under the new tag, so the pin is dropped, and the installer says so.
    moved = run_install(tmp_path, "0.9.9")
    assert moved.returncode == 0, moved.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.9"
    assert "Dropped the digest pin" in moved.stderr and "--digest" in moved.stderr

    # Another version with its own digest pins that one.
    repinned = run_install(tmp_path, "0.9.10", CUSTOM_DOMAIN_IMAGE_DIGEST=OTHER_DIGEST)
    assert repinned.returncode == 0, repinned.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.10@{OTHER_DIGEST}"
    assert "Dropped" not in repinned.stderr


def test_digest_in_the_config_file_never_pins_an_explicit_version(tmp_path):
    """cloud-init writes the version and its digest; `custom-domain upgrade X`
    later gives only the version."""
    config = tmp_path / "install.env"
    config.write_text(f"CUSTOM_DOMAIN_VERSION=0.9.8\nCUSTOM_DOMAIN_IMAGE_DIGEST={DIGEST}\n")
    first = run_install(tmp_path, None, CUSTOM_DOMAIN_INSTALL_ENV=str(config))
    assert first.returncode == 0, first.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.8@{DIGEST}"
    upgraded = run_install(tmp_path, "0.9.9", CUSTOM_DOMAIN_INSTALL_ENV=str(config))
    assert upgraded.returncode == 0, upgraded.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.9"
    assert "Dropped the digest pin" in upgraded.stderr


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        ("0.9.9", "0123456789abcdef" * 4),  # no algorithm
        ("0.9.9", "sha512:" + "0123456789abcdef" * 4),
        ("0.9.9", "sha256:" + "0123456789ABCDEF" * 4),  # Docker digests are lowercase
        ("0.9.9", DIGEST[:-1]),
        ("0.9.9", DIGEST + "0"),
        ("0.9.9", DIGEST + "\nsha256:" + "0" * 64),
        ("0.9.9", DIGEST[:-4] + "|x;y"),
        ("0.9.9", "sha256:" + "g" * 64),
        (None, DIGEST),  # a digest pins one release: latest is not one
        ("latest", DIGEST),
    ],
)
def test_unusable_digest_stops_before_anything_changes(tmp_path, version, digest):
    result = run_install(tmp_path, version, CUSTOM_DOMAIN_IMAGE_DIGEST=digest)
    assert result.returncode == 1 and "CUSTOM_DOMAIN_IMAGE_DIGEST" in result.stderr
    assert not (tmp_path / "opt").exists()


def test_host_command_passes_the_digest_to_the_upgrade(tmp_path):
    """`custom-domain upgrade <version> --digest <digest>` reaches the installer,
    and an upgrade without --digest never takes one from the config file."""
    config = tmp_path / "install.env"
    config.write_text(f"CUSTOM_DOMAIN_VERSION=0.9.8\nCUSTOM_DOMAIN_IMAGE_DIGEST={DIGEST}\n")
    assert run_install(tmp_path, None, CUSTOM_DOMAIN_INSTALL_ENV=str(config)).returncode == 0
    stubs = tmp_path / "stubs"
    # The host command downloads the release's installer; serve this one.
    (stubs / "curl").write_text(
        '#!/usr/bin/env bash\nwhile [ "$#" -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        f'cp "{SCRIPT}" "$out"\n'
    )
    (stubs / "curl").chmod(0o755)
    environment = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CUSTOM_DOMAIN_BIN_DIR": str(tmp_path / "bin"),
        "CUSTOM_DOMAIN_SOURCE": str(ROOT),
        "CUSTOM_DOMAIN_SKIP_ROOT_CHECK": "1",
        "CUSTOM_DOMAIN_INSTALL_ENV": str(config),
        "SKIP_DOCKER_INSTALL": "1",
        "SKIP_FIREWALL": "1",
    }

    def upgrade(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(tmp_path / "bin" / "custom-domain"), "upgrade", *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    pinned = upgrade("0.9.9", "--digest", OTHER_DIGEST)
    assert pinned.returncode == 0, pinned.stderr + pinned.stdout
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.9@{OTHER_DIGEST}"
    unpinned = upgrade("0.9.10")
    assert unpinned.returncode == 0, unpinned.stderr + unpinned.stdout
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.10"
    assert "Dropped the digest pin" in unpinned.stderr
    refused = upgrade("0.9.11", f"--digest={DIGEST[:-1]}")
    assert refused.returncode == 1 and "CUSTOM_DOMAIN_IMAGE_DIGEST" in refused.stderr
    assert env_of(tmp_path)["CUSTOM_DOMAIN_IMAGE"] == f"{REPOSITORY}:0.9.10"
