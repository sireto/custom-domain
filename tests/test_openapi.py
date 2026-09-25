import json
from pathlib import Path

import pytest

from app.main import create_app

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def spec(pytestconfig):
    import os

    os.environ["ENABLE_LEGACY_API"] = "true"
    return create_app().openapi()


def test_v1_paths_and_methods(spec):
    paths = spec["paths"]
    assert set(paths["/v1/domains"]) == {"get", "post"}
    assert set(paths["/v1/domains/{domain_id}"]) == {"get", "delete"}
    assert set(paths["/v1/domains/{domain_id}/checks"]) == {"post"}
    scheme = spec["components"]["securitySchemes"]["ApplicationCredential"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    for path, methods in paths.items():
        if path.startswith("/v1"):
            for operation in methods.values():
                assert operation["security"] == [{"ApplicationCredential": []}], path


def test_v1_create_has_no_upstream_and_forbids_extra_fields(spec):
    create = spec["components"]["schemas"]["DomainCreate"]
    assert set(create["properties"]) == {"hostname", "reference", "metadata"}
    assert create["additionalProperties"] is False
    assert create["required"] == ["hostname", "reference"]


def test_examples_cover_the_lifecycle(spec):
    paths = spec["paths"]
    created = paths["/v1/domains"]["post"]["responses"]["201"]["content"]["application/json"]
    assert created["examples"]["registration"]["value"]["status"] == "pending_dns"
    shown = paths["/v1/domains/{domain_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    examples = shown["examples"]
    assert examples["waiting_for_dns"]["value"]["checks"][0]["error_code"] == "txt_record_not_found"
    assert examples["ready"]["value"]["status"] == "ready"
    drift = examples["dns_drift"]["value"]
    assert drift["status"] == "attention_required"
    assert drift["checks"][1]["error_code"] == "cname_target_mismatch"
    deleted = paths["/v1/domains/{domain_id}"]["delete"]["responses"]["202"]["content"][
        "application/json"
    ]
    assert deleted["example"]["status"] == "deleting" and deleted["example"]["dns_records"] == []
    conflict = paths["/v1/domains"]["post"]["responses"]["409"]["content"]["application/json"]
    assert conflict["example"]["error"]["code"] == "hostname_already_claimed"


def test_status_and_check_enums_are_published(spec):
    schemas = spec["components"]["schemas"]
    assert schemas["DomainStatus"]["enum"] == [
        "pending_dns",
        "provisioning",
        "ready",
        "attention_required",
        "suspended",
        "deleting",
    ]
    assert schemas["CheckType"]["enum"] == ["ownership", "routing", "certificate", "origin"]
    assert schemas["CheckStatus"]["enum"] == ["pending", "passing", "failing"]


def test_webhook_contract_is_published(spec):
    hooks = spec["webhooks"]
    assert set(hooks) == {
        "domain.ready",
        "domain.attention_required",
        "domain.recovered",
        "domain.deleted",
    }
    event = spec["components"]["schemas"]["WebhookEvent"]
    assert set(event["required"]) == {"id", "type", "created_at", "data"}
    ready = hooks["domain.ready"]["post"]
    assert "X-Custom-Domain-Signature" in ready["description"]
    assert ready["requestBody"]["content"]["application/json"]["example"]["type"] == "domain.ready"


def test_legacy_api_is_marked_deprecated(spec):
    for method in ("get", "post", "delete"):
        assert spec["paths"]["/domains"][method]["deprecated"] is True


def test_committed_contract_is_current(spec):
    committed = json.loads((REPO / "docs" / "openapi.json").read_text())
    assert committed == spec, "run: uv run custom-domain openapi export --output docs/openapi.json"
