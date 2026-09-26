"""The deployment files must agree with each other and with the code."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_production_compose_shares_the_edge_settings_the_gateway_checks():
    """The reconciler runs in the api and worker containers and writes
    EDGE_ASSERT_UPSTREAM into every route; the gateway in the edge container
    accepts only its own value. All three must therefore carry the same one,
    and the reconciling containers must know where the gateway is."""
    compose = yaml.safe_load((ROOT / "deploy" / "compose.production.yml").read_text())
    services = compose["services"]
    upstreams = {
        name: services[name]["environment"].get("EDGE_ASSERT_UPSTREAM")
        for name in ("api", "worker", "edge")
    }
    assert len(set(upstreams.values())) == 1 and None not in upstreams.values(), upstreams
    for name in ("api", "worker"):
        assert services[name]["environment"]["CADDY_ADMIN_URL"] == "http://edge:2019"
        assert services[name]["environment"]["ENABLE_LEGACY_API"] == "false"
    assert services["edge"]["environment"]["API_URL"] == "http://api:9000"
    # The management API and portal are published on the host's loopback only.
    assert services["api"]["ports"] == ["127.0.0.1:9000:9000"]
