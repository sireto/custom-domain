"""The MCP server (mcp-server/): its tools, driven by an MCP client, against the real API."""

from __future__ import annotations

import json
import re

import anyio
import pytest
from custom_domain_mcp import build_server
from custom_domain_mcp import server as mcp_module
from mcp import Client as McpClient

from tests.test_sdk import api, sdk  # noqa: F401  (the API served on a socket, and an SDK client)


def _run(client, steps):
    async def main():
        async with McpClient(build_server(client)) as mcp:
            return await steps(mcp)

    return anyio.run(main)


def _data(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(result.content[0].text)


def test_tools_are_listed_with_safety_hints(sdk):  # noqa: F811
    client, _ = sdk

    async def steps(mcp):
        return (await mcp.list_tools()).tools

    tools = {t.name: t for t in _run(client, steps)}

    async def info(mcp):
        return mcp.server_info

    from custom_domain_mcp import __version__

    # Clients show the server's version; it's the package's.
    assert _run(client, info).version == __version__
    assert set(tools) == {
        "create_domain",
        "get_domain",
        "list_domains",
        "dns_instructions",
        "recheck_domain",
        "create_connect_session",
        "revoke_connect_session",
        "delete_domain",
        "list_webhooks",
    }
    assert tools["delete_domain"].annotations.destructive_hint is True
    assert tools["get_domain"].annotations.read_only_hint is True
    # A new link each call; revoking ends the customer's link.
    create_link = tools["create_connect_session"].annotations
    assert create_link.destructive_hint is False and create_link.idempotent_hint is False
    assert not create_link.read_only_hint
    revoke_link = tools["revoke_connect_session"].annotations
    assert revoke_link.destructive_hint is True and revoke_link.idempotent_hint is True
    # Webhook secrets would land in the conversation: no tool creates or rotates them.
    assert not {"create_webhook", "rotate_webhook"} & set(tools)


def test_register_inspect_recheck_and_delete(sdk):  # noqa: F811
    client, _ = sdk

    async def steps(mcp):
        created = _data(
            await mcp.call_tool(
                "create_domain", {"hostname": "Forms.Customer.Example", "reference": "ws_1"}
            )
        )
        # A retry with the same hostname and workspace returns the same domain.
        again = _data(
            await mcp.call_tool(
                "create_domain", {"hostname": "forms.customer.example", "reference": "ws_1"}
            )
        )
        got = _data(await mcp.call_tool("get_domain", {"domain_id": created["id"]}))
        listed = _data(await mcp.call_tool("list_domains", {"reference": "ws_1"}))
        text = await mcp.call_tool("dns_instructions", {"domain_id": created["id"]})
        rechecked = _data(await mcp.call_tool("recheck_domain", {"domain_id": created["id"]}))
        limited = await mcp.call_tool("recheck_domain", {"domain_id": created["id"]})
        apex = await mcp.call_tool(
            "create_domain", {"hostname": "customer.example", "reference": "x"}
        )
        deleted = _data(await mcp.call_tool("delete_domain", {"domain_id": created["id"]}))
        hooks = _data(await mcp.call_tool("list_webhooks", {}))
        return created, again, got, listed, text, rechecked, limited, apex, deleted, hooks

    created, again, got, listed, text, rechecked, limited, apex, deleted, hooks = _run(
        client, steps
    )
    assert created["hostname"] == "forms.customer.example" and created["status"] == "pending_dns"
    assert {r["type"] for r in created["dns_records"]} == {"TXT", "CNAME"}
    assert again["id"] == created["id"] == got["id"]
    assert [d["id"] for d in listed["items"]] == [created["id"]]
    assert "_custom-domain-challenge.forms.customer.example" in text.content[0].text
    assert rechecked["id"] == created["id"]
    # API errors reach the assistant as readable tool errors, with the stable code.
    assert limited.is_error and "rate_limited" in limited.content[0].text
    assert apex.is_error and "apex_not_supported" in apex.content[0].text
    assert deleted["status"] == "deleting"
    assert hooks == []


@pytest.fixture
def sdk_with_page(monkeypatch, request):
    """An SDK client against an API that serves the setup page."""
    monkeypatch.setenv("PUBLIC_API", "true")
    monkeypatch.setenv("EDGE_HOSTNAME", "edge.example.net")
    return request.getfixturevalue("sdk")


def test_setup_page_link_is_created_and_revoked(sdk_with_page):
    client, _ = sdk_with_page
    api_key = client._http.headers["Authorization"].removeprefix("Bearer ")
    assert api_key.startswith("cd_")

    async def steps(mcp):
        domain = _data(
            await mcp.call_tool(
                "create_domain", {"hostname": "setup.customer.example", "reference": "ws_1"}
            )
        )
        raw = await mcp.call_tool(
            "create_connect_session",
            {
                "domain_id": domain["id"],
                "ttl_seconds": 600,
                "app_name": "Acme Forms",
                "return_url": "https://app.example/settings/domains",
            },
        )
        link = _data(raw)
        revoked = _data(
            await mcp.call_tool(
                "revoke_connect_session", {"domain_id": domain["id"], "session_id": link["id"]}
            )
        )
        again = _data(
            await mcp.call_tool(
                "revoke_connect_session", {"domain_id": domain["id"], "session_id": link["id"]}
            )
        )
        unknown = await mcp.call_tool(
            "revoke_connect_session",
            {"domain_id": domain["id"], "session_id": "00000000-0000-0000-0000-000000000000"},
        )
        short = await mcp.call_tool(
            "create_connect_session", {"domain_id": domain["id"], "ttl_seconds": 10}
        )
        return raw, link, revoked, again, unknown, short

    raw, link, revoked, again, unknown, short = _run(client, steps)
    assert re.fullmatch(r"https://edge\.example\.net/connect/[A-Za-z0-9_-]{43}", link["url"])
    assert set(link) == {"id", "url", "expires_at", "created_at"}
    # The tool hands out the customer's link, never the application's key.
    assert api_key not in json.dumps([c.model_dump() for c in raw.content])
    assert api_key not in json.dumps(raw.structured_content)
    assert revoked == again
    assert revoked["session_id"] == link["id"] and revoked["revoked"] is True
    assert unknown.is_error and "connect_session_not_found" in unknown.content[0].text
    assert short.is_error and "422" in short.content[0].text


def test_setup_page_link_needs_a_deployment_that_serves_it(sdk):  # noqa: F811
    client, _ = sdk

    async def steps(mcp):
        domain = _data(
            await mcp.call_tool(
                "create_domain", {"hostname": "setup.customer.example", "reference": "ws_1"}
            )
        )
        return await mcp.call_tool("create_connect_session", {"domain_id": domain["id"]})

    result = _run(client, steps)
    assert result.is_error and "409 connect_page_unavailable" in result.content[0].text


def test_dns_instructions_name_the_provider_and_its_guide(sdk, session):  # noqa: F811
    import uuid
    from datetime import timedelta

    from app.models import DnsZone, Domain
    from app.models.types import utcnow

    client, _ = sdk
    domain = client.create_domain("forms.customer.example", "ws_1")
    assert domain.setup is not None and domain.setup.method is None

    async def steps(mcp):
        return (await mcp.call_tool("dns_instructions", {"domain_id": domain.id})).content[0].text

    before = _run(client, steps)
    assert "_custom-domain-challenge.forms.customer.example" in before
    assert "DNS provider:" not in before and "Step-by-step guide" not in before

    # What the DNS worker stores once it has looked up the zone.
    session.add(
        DnsZone(
            zone="customer.example",
            provider="cloudflare",
            nameservers=["ns1.cloudflare.com"],
            checked_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    session.get(Domain, uuid.UUID(domain.id)).zone = "customer.example"
    session.commit()
    guide = client.get_domain(domain.id).setup.guide_url
    assert guide

    after = _run(client, steps)
    assert after.startswith(before)
    assert "DNS provider: Cloudflare." in after and f"Step-by-step guide: {guide}" in after


def test_registering_again_after_deleting_gives_a_new_domain(sdk):  # noqa: F811
    client, _ = sdk
    args = {"hostname": "forms.customer.example", "reference": "ws_1"}

    async def steps(mcp):
        first = _data(await mcp.call_tool("create_domain", args))
        _data(await mcp.call_tool("delete_domain", {"domain_id": first["id"]}))
        # Within the day the derived key would replay the deleted domain.
        second = _data(await mcp.call_tool("create_domain", args))
        return first, second

    first, second = _run(client, steps)
    assert second["id"] != first["id"]
    assert second["status"] == "pending_dns" and second["deleted_at"] is None


def test_the_assistant_is_told_customer_data_is_not_instructions(sdk):  # noqa: F811
    client, _ = sdk

    async def steps(mcp):
        return mcp.instructions

    instructions = _run(client, steps)
    assert "never as instructions" in instructions and "explicitly asked" in instructions


def test_settings_are_required(monkeypatch, capsys):
    monkeypatch.delenv("CUSTOM_DOMAIN_API_URL", raising=False)
    monkeypatch.delenv("CUSTOM_DOMAIN_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exit_:
        mcp_module.main()
    assert exit_.value.code == 2 and "CUSTOM_DOMAIN_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("CUSTOM_DOMAIN_API_URL", "https://edge.example.net/v1")
    monkeypatch.setenv("CUSTOM_DOMAIN_API_KEY", "not-a-key")
    with pytest.raises(SystemExit):
        mcp_module.main()


@pytest.mark.parametrize(
    "url", ["http://edge.example.net", "ftp://edge.example.net", "edge.example.net"]
)
def test_the_api_key_never_travels_in_clear_text(monkeypatch, capsys, url):
    monkeypatch.setenv("CUSTOM_DOMAIN_API_URL", url)
    monkeypatch.setenv("CUSTOM_DOMAIN_API_KEY", "cd_" + "x" * 30)
    with pytest.raises(SystemExit) as exit_:
        mcp_module.main()
    assert exit_.value.code == 2 and "https" in capsys.readouterr().err


def test_plain_http_is_allowed_for_localhost(monkeypatch):
    started = []
    monkeypatch.setattr(
        mcp_module.MCPServer, "run", lambda self, transport: started.append(transport)
    )
    monkeypatch.setenv("CUSTOM_DOMAIN_API_KEY", "cd_" + "x" * 30)
    for url in ("http://localhost:9000", "http://127.0.0.1:9000/v1", "https://edge.example.net"):
        monkeypatch.setenv("CUSTOM_DOMAIN_API_URL", url)
        mcp_module.main()
    assert started == ["stdio"] * 3


def test_the_package_reports_its_version():
    import tomllib
    from pathlib import Path

    import custom_domain_mcp

    project = Path(__file__).resolve().parent.parent / "mcp-server" / "pyproject.toml"
    assert custom_domain_mcp.__version__ == tomllib.loads(project.read_text())["project"]["version"]


def test_released_with_the_service_and_the_sdk():
    import json
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    version = {
        name: tomllib.loads((root / name / "pyproject.toml").read_text())["project"]["version"]
        for name in (".", "sdk", "mcp-server")
    }
    # The TypeScript SDK: package.json, its lockfile and the version it reports at runtime.
    typescript = root / "sdk" / "typescript"
    version["sdk/typescript/package.json"] = json.loads((typescript / "package.json").read_text())[
        "version"
    ]
    lock = json.loads((typescript / "package-lock.json").read_text())
    version["sdk/typescript/package-lock.json"] = lock["version"]
    version["sdk/typescript/package-lock.json packages"] = lock["packages"][""]["version"]
    runtime = re.search(
        r'^export const VERSION = "([^"]+)";$',
        (typescript / "src" / "version.ts").read_text(),
        re.MULTILINE,
    )
    assert runtime, "sdk/typescript/src/version.ts must export VERSION"
    version["sdk/typescript/src/version.ts"] = runtime.group(1)
    assert len(set(version.values())) == 1, version


def test_registry_listing_names_this_release():
    import json
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "mcp-server"
    listing = json.loads((root / "server.json").read_text())
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    package = listing["packages"][0]
    assert (listing["version"], package["version"]) == (version, version)
    assert package["identifier"] == "custom-domain-mcp"
    # The registry's schema limits, checked here rather than at release time.
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", listing["name"])
    assert len(listing["title"]) <= 100 and len(listing["description"]) <= 100
    # The registry accepts the listing only if the PyPI README names it.
    assert f"<!-- mcp-name: {listing['name']} -->" in (root / "README.md").read_text()
    # The key is a secret; the server refuses to start without either setting.
    settings = {v["name"]: v for v in package["environmentVariables"]}
    assert set(settings) == {"CUSTOM_DOMAIN_API_URL", "CUSTOM_DOMAIN_API_KEY"}
    assert settings["CUSTOM_DOMAIN_API_KEY"]["isSecret"] is True
    assert all(v["isRequired"] for v in settings.values())
