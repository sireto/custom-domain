# custom-domain-mcp

An [MCP](https://modelcontextprotocol.io) server for the Custom Domain API.
It lets an AI assistant (Claude, Cursor, any MCP client) register your
customers' hostnames, show them their DNS records or a link to a setup page,
check why a domain isn't live yet, and recheck or delete it. It acts as one application, with that
application's own API key.

<!-- mcp-name: io.github.sireto/custom-domain -->

## Setup

```bash
uvx custom-domain-mcp        # or: pip install custom-domain-mcp
```

It reads two settings from the environment:

| Setting | Value |
|---|---|
| `CUSTOM_DOMAIN_API_URL` | your edge, such as `https://edge.example.net` (https only; plain http just for `localhost`) |
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
| `dns_instructions` | The records as text for the customer, with help for each, and the detected DNS provider's guide. |
| `create_connect_session` | A link to the setup page for one domain (records, live checks, one-step setup when the provider supports it), with its expiry. |
| `revoke_connect_session` | End a setup page link now (marked destructive). |
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

**A setup page link is a credential for one domain.** Whoever holds it can
see that domain's setup and status, ask for a check and start one-step
setup; nothing else, and never the API key. The server tells the assistant
to give it only to the customer who owns the hostname. The deployment must
serve the page (`PUBLIC_API=true` and `EDGE_HOSTNAME`); otherwise
`create_connect_session` reports `connect_page_unavailable`.

**Customer data is data, not instructions.** Hostnames, workspace
references, metadata and check messages come from your application's
customers and from DNS. The server tells the assistant to report them,
never to follow them, and to call `delete_domain` only when you explicitly
asked for that domain to be deleted, repeating its hostname back to you
first. It also tells the assistant to create a setup page link only when you
ask for one and to hand it to you, never to send it to an address, URL or
channel found in domain data: metadata is customer-controlled and must not
steer where a credential goes. Your MCP client's own confirmation for destructive tools adds a
second check.

The server's instructions also tell the assistant the one rule integration
code must follow: select the tenant from the verified
`X-Custom-Domain-Assertion`, never from `Host`
([verifying requests](https://github.com/sireto/custom-domain/blob/main/docs/edge-routing.md)).
