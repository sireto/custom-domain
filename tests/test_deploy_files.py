"""The deployment files must agree with each other and with the code."""

import re
from pathlib import Path

import pytest
import yaml

from app.edge import config as edge_config
from app.edge.gateway import _expected_tls
from app.edge.settings import EdgeSettings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose.production.yml"
ENV_EXAMPLE = ROOT / "deploy" / "env.production.example"

# Settings the reconciler (api, worker) writes into the edge configuration and
# the gateway (edge) checks against its own environment. Any of them defined
# per service must be defined identically on all three; a mismatch makes the
# gateway reject every reconciliation.
GATEWAY_CHECKED = ("EDGE_ASSERT_UPSTREAM", "EDGE_ASK_URL")

# Every setting EdgeSettings reads that changes what the gateway accepts. The
# edge must be given each one explicitly (it has no env_file).
GATEWAY_INPUTS = (
    "EDGE_ASSERT_UPSTREAM",
    "EDGE_ASK_URL",
    "EDGE_TOKEN",
    "DISABLE_HTTPS",
    "ACME_EMAIL",
    "EDGE_TLS_ISSUER",
    "EDGE_HTTPS_PORT",
    "EDGE_HTTP_PORT",
)

# What the internet-facing edge must never hold.
EDGE_FORBIDDEN = (
    "SERVICE_SECRET_KEY",
    "OPERATOR_API_TOKEN",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "EDGE_ASSERTION_KEYS",
    "PORTAL_PASSWORD",
)
EDGE_FORBIDDEN_PREFIXES = ("DOMAIN_CONNECT_",)

_SUBSTITUTION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")


def compose_services() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())["services"]


def parse_env_file(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def substitute(value: str, dotenv: dict[str, str]) -> str:
    """Compose's ${VAR}, ${VAR:-default}, ${VAR-default} and ${VAR:?message}."""

    def replace(match: re.Match) -> str:
        name, op, arg = match.group(1), match.group(2), match.group(3)
        current = dotenv.get(name)
        if op in (":-", ":?") and not current or op in ("-", "?") and current is None:
            if op.endswith("?"):
                raise AssertionError(f"{name} is required: {arg}")
            return arg
        return current or ""

    return _SUBSTITUTION.sub(replace, value)


def effective_env(service: dict, dotenv: dict[str, str]) -> dict[str, str]:
    """What a container's processes see: env_file, then the substituted environment."""
    env = dict(dotenv) if service.get("env_file") else {}
    for key, value in (service.get("environment") or {}).items():
        env[key] = substitute(str(value), dotenv)
    return env


def gateway_view(env: dict[str, str]) -> dict:
    """Everything that decides whether the gateway accepts the reconciler's output."""
    settings = EdgeSettings.from_env(env)
    return {
        "http": edge_config.http_app(settings, []),
        "assert": edge_config.assertion_subrequest(settings),
        "tls": None if settings.disable_https else _expected_tls(settings),
        "disable_https": settings.disable_https,
    }


def _example() -> dict[str, str]:
    env = parse_env_file(ENV_EXAMPLE.read_text())
    # The example's placeholders are not long enough for validation.
    env["EDGE_ASSERTION_KEYS"] = "1:" + "k" * 40
    env["CADDY_REDIS_ENCRYPTION_KEY"] = "e" * 32
    return env


def _without(env: dict[str, str], *names: str) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in names}


DOTENV_VARIANTS = {
    "example": _example(),
    "no acme email or edge token": _without(_example(), "ACME_EMAIL", "EDGE_TOKEN"),
    "empty acme email": {**_example(), "ACME_EMAIL": ""},
    "https disabled": {**_example(), "DISABLE_HTTPS": "true"},
    "internal issuer, other ports": {
        **_example(),
        "EDGE_TLS_ISSUER": "internal",
        "EDGE_HTTPS_PORT": "8443",
        "EDGE_HTTP_PORT": "8080",
    },
    "redis tuned": {**_example(), "CADDY_REDIS_DB": "2", "CADDY_REDIS_KEY_PREFIX": "certs"},
}


def test_production_compose_shares_the_edge_settings_the_gateway_checks():
    services = compose_services()
    for key in GATEWAY_CHECKED:
        values = {
            name: services[name]["environment"].get(key) for name in ("api", "worker", "edge")
        }
        assert None not in values.values(), f"{key} must be set on api, worker and edge: {values}"
        assert len(set(values.values())) == 1, f"{key} differs between services: {values}"
    # Any other EDGE_* or DISABLE_HTTPS/ACME setting given literally per service
    # must agree too (EDGE_RECONCILE_ENABLED only says whether the service
    # reconciles, which the edge never does).
    per_service = {name: services[name]["environment"] for name in ("api", "worker", "edge")}
    keys = {
        k
        for env in per_service.values()
        for k in env
        if k.startswith(("EDGE_", "ACME_", "DISABLE_HTTPS"))
    }
    for key in keys - {"EDGE_ASK_TRUSTED_HOSTS", "EDGE_RECONCILE_ENABLED"}:
        values = {name: env[key] for name, env in per_service.items() if key in env}
        assert len(set(values.values())) == 1, f"{key} differs between services: {values}"
    for name in ("api", "worker"):
        assert services[name]["environment"]["CADDY_ADMIN_URL"] == "http://edge:2019"
        assert services[name]["environment"]["ENABLE_LEGACY_API"] == "false"
    assert services["edge"]["environment"]["API_URL"] == "http://api:9000"
    # The management API and portal are published on the host's loopback only.
    assert services["api"]["ports"] == ["127.0.0.1:9000:9000"]


@pytest.mark.parametrize("variant", sorted(DOTENV_VARIANTS))
def test_api_worker_and_edge_see_the_same_gateway_settings(variant):
    """The edge reads its settings through ${...}, api and worker through env_file:
    for any .env, the gateway must check exactly what the reconciler builds."""
    dotenv = DOTENV_VARIANTS[variant]
    services = compose_services()
    views = {
        name: gateway_view(effective_env(services[name], dotenv))
        for name in ("api", "worker", "edge")
    }
    assert views["api"] == views["worker"] == views["edge"], variant
    edge_env = effective_env(services["edge"], dotenv)
    settings = EdgeSettings.from_env(edge_env)
    # The edge starts: it stores certificates in the shared Redis with the
    # operator's prefix, and needs no assertion keys because it never reconciles.
    assert not settings.reconcile_enabled and not settings.assertion_keys
    storage = settings.storage_config()
    assert storage is not None and storage["password"] == dotenv["CADDY_REDIS_PASSWORD"]
    assert storage["encryption_key"] == dotenv["CADDY_REDIS_ENCRYPTION_KEY"]
    assert storage["key_prefix"] == dotenv.get("CADDY_REDIS_KEY_PREFIX", "caddy")
    assert storage["db"] == int(dotenv.get("CADDY_REDIS_DB", "0"))


def test_edge_is_given_every_gateway_input_explicitly():
    edge = compose_services()["edge"]["environment"]
    missing = [name for name in GATEWAY_INPUTS if name not in edge]
    assert not missing, f"the edge has no env_file; give it {missing}"


def test_edge_receives_only_its_own_settings():
    """The internet-facing edge never holds the database, signing or operator secrets."""
    edge = compose_services()["edge"]
    assert "env_file" not in edge, "the edge must list its settings, not load all of .env"
    for dotenv in DOTENV_VARIANTS.values():
        # A distinct value per secret (the example's placeholders repeat).
        dotenv = {
            **dotenv,
            **{name: f"secret-{name.lower()}-0123456789abcdef" for name in EDGE_FORBIDDEN},
            "EDGE_ASSERTION_KEYS": "1:secret-assertion-key-0123456789abcdef",
            "DOMAIN_CONNECT_SIGNER": "local",
            "DOMAIN_CONNECT_PROVIDER_ID": "example.net",
        }
        env = effective_env(edge, dotenv)
        leaked = [
            key for key in env if key in EDGE_FORBIDDEN or key.startswith(EDGE_FORBIDDEN_PREFIXES)
        ]
        assert not leaked, f"the edge must not receive {leaked}"
        secrets = {dotenv[k] for k in EDGE_FORBIDDEN if dotenv.get(k)}
        assert not secrets & set(env.values()), "a forbidden secret reaches the edge"


def test_every_production_service_rotates_its_log():
    """With INFO logging on, an unrotated json-file log grows without bound."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    for name, service in compose["services"].items():
        logging = service.get("logging")
        assert logging and logging["driver"] == "json-file", name
        options = logging["options"]
        assert options["max-size"].endswith("m") and int(options["max-size"][:-1]) <= 50, name
        assert 1 <= int(options["max-file"]) <= 10, name
