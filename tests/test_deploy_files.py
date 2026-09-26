"""The deployment files must agree with each other and with the code."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# Settings the reconciler (api, worker) writes into the edge configuration and
# the gateway (edge) checks against its own environment. Any of them defined
# per service must be defined identically on all three; a mismatch makes the
# gateway reject every reconciliation.
GATEWAY_CHECKED = ("EDGE_ASSERT_UPSTREAM", "EDGE_ASK_URL")


def test_production_compose_shares_the_edge_settings_the_gateway_checks():
    compose = yaml.safe_load((ROOT / "deploy" / "compose.production.yml").read_text())
    services = compose["services"]
    for key in GATEWAY_CHECKED:
        values = {
            name: services[name]["environment"].get(key) for name in ("api", "worker", "edge")
        }
        assert None not in values.values(), f"{key} must be set on api, worker and edge: {values}"
        assert len(set(values.values())) == 1, f"{key} differs between services: {values}"
    # Any other EDGE_* or DISABLE_HTTPS/ACME setting given per service must agree too.
    per_service = {name: services[name]["environment"] for name in ("api", "worker", "edge")}
    keys = {
        k
        for env in per_service.values()
        for k in env
        if k.startswith(("EDGE_", "ACME_", "DISABLE_HTTPS"))
    }
    for key in keys - {"EDGE_ASK_TRUSTED_HOSTS"}:
        values = {name: env[key] for name, env in per_service.items() if key in env}
        assert len(set(values.values())) == 1, f"{key} differs between services: {values}"
    for name in ("api", "worker"):
        assert services[name]["environment"]["CADDY_ADMIN_URL"] == "http://edge:2019"
        assert services[name]["environment"]["ENABLE_LEGACY_API"] == "false"
    assert services["edge"]["environment"]["API_URL"] == "http://api:9000"
    # The management API and portal are published on the host's loopback only.
    assert services["api"]["ports"] == ["127.0.0.1:9000:9000"]
