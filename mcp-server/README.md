# custom-domain-mcp

An [MCP](https://modelcontextprotocol.io) server for the Custom Domain API.
It lets an AI assistant (Claude, Cursor, any MCP client) register your
customers' hostnames, show them their DNS records, check why a domain isn't
live yet, and recheck or delete it. It acts as one application, with that
application's own API key.

## Setup

```bash
uvx custom-domain-mcp        # or: pip install custom-domain-mcp
```

It reads two settings from the environment:

| Setting | Value |
|---|---|
| `CUSTOM_DOMAIN_API_URL` | your edge, such as `https://edge.example.net` (on the hosted Free plan: `https://edge.customdomainapi.com`) |
| `CUSTOM_DOMAIN_API_KEY` | an application API key (`cd_…`) |

Claude Code:

```bash
claude mcp add custom-domain --env CUSTOM_DOMAIN_API_URL=https://edge.example.net \
  --env CUSTOM_DOMAIN_API_KEY=cd_... -- uvx custom-domain-mcp
```

Claude Desktop, Cursor and other clients (`mcpServers` in their config):

```json
{
  "mcpServers": {
    "custom-domain": {
      "command": "uvx",
      "args": ["custom-domain-mcp"],
      "env": {
        "CUSTOM_DOMAIN_API_URL": "https://edge.example.net",
        "CUSTOM_DOMAIN_API_KEY": "cd_..."
      }
    }
  }
}
```

## Tools

| Tool | Does |
|---|---|
| `create_domain` | Register a customer's hostname for a workspace; returns its DNS records. Safe to retry. |
| `get_domain` | A domain with its status, records and the four checks. |
| `list_domains` | Domains, filtered by workspace or status, paged. |
| `dns_instructions` | The records as text for the customer, with help for each. |
| `recheck_domain` | Run the checks again after a DNS fix (once a minute per domain). |
| `delete_domain` | Stop serving a hostname and delete it (marked destructive). |
| `list_webhooks` | Webhook subscriptions, without secrets. |

## What it can't do

- **No operator actions.** It never holds an operator token, only the
  application's key, so it can do exactly what the application's backend
  can.
- **No webhook creation or rotation.** Their responses carry the signing
  secret, which would end up in the assistant's conversation. Create
  webhooks through the API or the portal.

The server's instructions also tell the assistant the one rule integration
code must follow: select the tenant from the verified
`X-Custom-Domain-Assertion`, never from `Host`
([verifying requests](https://github.com/sireto/custom-domain/blob/main/docs/edge-routing.md)).
