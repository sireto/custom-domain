"""The MCP server (mcp-server/): its tools, driven by an MCP client, against the real API."""

from __future__ import annotations

import json

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
    assert set(tools) == {
        "create_domain",
        "get_domain",
        "list_domains",
        "dns_instructions",
        "recheck_domain",
        "delete_domain",
        "list_webhooks",
    }
    assert tools["delete_domain"].annotations.destructive_hint is True
    assert tools["get_domain"].annotations.read_only_hint is True
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


def test_released_with_the_service_and_the_sdk():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    version = {
        name: tomllib.loads((root / name / "pyproject.toml").read_text())["project"]["version"]
        for name in (".", "sdk", "mcp-server")
    }
    assert len(set(version.values())) == 1, version
