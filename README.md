# Custom Domain

Custom domains for multi-tenant SaaS products. Applications register their
customers' hostnames through an API, hand back exact DNS instructions, and are
told when each hostname is verified, certified and serving. The edge (Caddy)
obtains certificates on demand and forwards every request to the application's
origin with a signed assertion that names the workspace the hostname belongs
to, so the application never selects a tenant from the `Host` header.

Several applications share one deployment. Each has its own credentials, its
own verified origin and its own hostnames; nothing an application does can
affect another's.

Integration guides (quickstart, API, verifying requests, webhooks, and a
brief for AI coding agents) are at
[customdomainapi.com/docs](https://customdomainapi.com/docs/), also as
Markdown for agents ([llms.txt](https://customdomainapi.com/llms.txt)). The
same service is run as a hosted option at
[customdomainapi.com](https://customdomainapi.com).

## How it works

1. The operator creates an application, registers its origin and proves
   control of it (the origin serves a token at
   `/.well-known/custom-domain-origin-verification`), then issues an API
   credential.
2. The application calls `POST /v1/domains` with a customer hostname and an
   opaque workspace reference. The answer carries the two records the customer
   must publish: a TXT record proving ownership and a CNAME to the edge.
3. The lifecycle worker checks DNS, obtains the certificate through the edge
   and asks the origin, through the edge, which workspace it serves for the
   hostname. The domain becomes `ready` only when ownership, routing,
   certificate and origin checks all pass. Drift later moves it to
   `attention_required`; a lost TXT record suspends it after 24 hours.
4. Every request the edge proxies carries `X-Custom-Domain-Assertion`, an
   HMAC-signed token binding the request to the application, domain, workspace
   reference and hostname. The SDK middleware verifies it and exposes the
   workspace to the application.
5. Status changes are delivered as signed webhooks (`domain.ready`,
   `domain.attention_required`, `domain.recovered`, `domain.deleted`), with
   history and replay. Deleting a domain stops service on the next request.

Only exact customer subdomains are supported (no apex or wildcard names).

## Quick start (single container)

Create a `.env` file:

```
DATABASE_URL=sqlite:///data/custom_domain.db
ENABLE_LEGACY_API=false
EDGE_ASSERTION_KEYS=1:<at least 32 random characters>
ACME_EMAIL=ops@example.com
CADDY_STORAGE=file
```

`EDGE_ASSERTION_KEYS` signs the assertions the edge attaches to proxied
requests; generate the secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Use a PostgreSQL URL such as `postgresql+psycopg://user:password@db:5432/custom_domain`
for anything beyond a trial. All settings are listed in [.env_example](.env_example).

Run the image:

```bash
docker volume create https_data && docker volume create https_db && docker volume create https_domains
docker compose up -d
```

The bundled [docker-compose.yml](docker-compose.yml) publishes ports 80 and
443 and binds the management API to `127.0.0.1:9000`. With `PUBLIC_API=true`
and `EDGE_HOSTNAME` set, the edge also serves the v1 API at
`https://<EDGE_HOSTNAME>/v1` for applications (nothing else of the API is
routed); the production installer turns this on. Swagger UI is at
**http://localhost:9000/v1/docs**; the OpenAPI document is served at
`/v1/openapi.json` and committed as [docs/openapi.json](docs/openapi.json).

For production, use [deploy/compose.production.yml](deploy/compose.production.yml):
PostgreSQL, Redis-backed certificate storage, and separate API, worker and edge
containers with the edge's admin API behind a validating gateway. The
walkthrough, backups, monitoring and rollback are in [docs/deployment.md](docs/deployment.md).

## Try it locally

[deploy/compose.local.yml](deploy/compose.local.yml) runs the whole service
on one machine with nothing else: Caddy with its own private CA, the API and
workers, and the sample SaaS origin as a second container. The DNS checks
are answered from the records the service issued (`DNS_VERIFICATION_MODE=local`,
refused unless the edge is local too), so invented hostnames under
`localtest.me` (which resolves to 127.0.0.1) go through the real lifecycle.

```bash
docker compose -f deploy/compose.local.yml up -d --build
docker compose -f deploy/compose.local.yml exec custom-domain custom-domain dev demo
```

The demo creates the sample application, registers and verifies its origin,
issues a credential (printed once) and registers `alpha.sample.localtest.me`
and `beta.sample.localtest.me` for two workspaces, then waits for them to
become `ready`. Open https://alpha.sample.localtest.me/ and
https://beta.sample.localtest.me/: each shows its own workspace, served
through Caddy with a signed assertion. The certificate is signed by the local
CA; trust it or fetch it for curl:

```bash
docker compose -f deploy/compose.local.yml cp custom-domain:/var/lib/custom-domain/local-ca.crt local-ca.crt
curl --cacert local-ca.crt https://alpha.sample.localtest.me/
```

The management API is at http://127.0.0.1:9000/v1/docs; use the printed
credential with the SDK against it. `docker compose -f deploy/compose.local.yml down -v`
removes everything.

## Hosting on a cloud server

A single small server runs everything, with fixed IPv4 and IPv6 addresses
for customers' CNAMEs to resolve to. Each guide takes about fifteen minutes,
ending with the portal open at `https://<edge hostname>/portal` and the API
at `https://<edge hostname>/v1`.

| Cloud | How | Guide |
|---|---|---|
| Azure | [![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fsireto%2Fcustom-domain%2Fmain%2Fdeploy%2Fazure%2Fazuredeploy.json) (ARM/Bicep template) | [docs/hosting-azure.md](docs/hosting-azure.md) |
| AWS | CloudFormation stack ([deploy/aws/custom-domain.yaml](deploy/aws/custom-domain.yaml)) | [docs/hosting-aws.md](docs/hosting-aws.md) |
| Google Cloud | [![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https%3A%2F%2Fgithub.com%2Fsireto%2Fcustom-domain&cloudshell_git_branch=main&cloudshell_tutorial=deploy%2Fgcp%2Ftutorial.md&show=terminal) (guided script) | [docs/hosting-gcp.md](docs/hosting-gcp.md) |
| Hetzner Cloud | paste [deploy/cloud-init.yaml](deploy/cloud-init.yaml) as the server's cloud config | [docs/hosting-hetzner.md](docs/hosting-hetzner.md) |
| DigitalOcean | paste [deploy/cloud-init.yaml](deploy/cloud-init.yaml) as the Droplet's user data | [docs/hosting-digitalocean.md](docs/hosting-digitalocean.md) |

All of them run the same installer at a released version: Docker, generated
configuration and secrets, and the production layout, unattended.

The same script runs on any Ubuntu or Debian host as root:
`bash deploy/install.sh`, and re-running it (or `custom-domain upgrade <version>`)
is the upgrade path. Afterwards `custom-domain doctor` checks the
database and migrations, the edge gateway, the certificate authority, the
reconciler, and that each application's CNAME target reaches the edge.

## Operating it

Every operator action is available in two equivalent forms: the
`custom-domain` command, and the **portal** at `/portal` of the management
API. Both call the same code, so they never diverge.

**The portal** (set `PORTAL_PASSWORD` to enable it) is where most operators
start. It shows a setup checklist for each application, and for each
customer hostname the two DNS records with what public DNS currently returns
for them. The Edge & DNS page lists every name the edge answers for and
checks that it reaches this server. Everything below can be done there too,
including deletes, webhooks and the legacy import. Reach it at
`https://<EDGE_HOSTNAME>/portal` from the addresses in `PORTAL_ALLOWED_IPS`,
or through an SSH tunnel to port 9000 ([docs/portal.md](docs/portal.md)).

**The command** runs inside the API container (the installer puts a
`custom-domain` wrapper on the host), or with `uv run custom-domain` in a
checkout:

```bash
custom-domain application create --slug acme --name "Acme" --cname-target acme.edge.example.net
custom-domain origin register --application acme --host app.acme.example --scheme https --port 443
custom-domain origin verify --application acme --host app.acme.example --activate
custom-domain credential issue --application acme --label backend
```

`--cname-target` is the name customers point their CNAME at; it must resolve
to this deployment's edge, and defaults to `EDGE_HOSTNAME`. `origin register`
prints the verification token the application must serve before `origin
verify` succeeds. Credentials are shown once and can be rotated with a grace
period (`credential rotate`).

| Area | Commands |
|---|---|
| Applications | `application list [--deleted]`, `rename`, `set-cname-target [--reissue-claims]`, `set-workspace-probe`, `delete --confirm <slug> [--delete-domains]` |
| Origins | `origin list`, `register`, `verify`, `activate`, `retire`, `delete` |
| Credentials | `credential list`, `issue`, `rotate`, `revoke`, `delete` |
| Edge | `edge config`, `edge reconcile` (the Caddy configuration derived from the database) |
| Operations | `doctor` (checks the whole deployment), `upgrade <version>` (host wrapper), `db upgrade`, `worker run`, `domain purge-tombstones`, `legacy import` |

Deleting keeps the audit trail: a deleted hostname, or every hostname of a
deleted application, is kept as a tombstone with its history for 90 days,
then removed by `domain purge-tombstones`. `custom-domain --help` lists
every command and option.

## Integrating an application

The Python SDK in [sdk/](sdk/README.md) (`custom-domain-sdk`, released at the
same version as the service image) has the API client, the assertion
verifier, an ASGI middleware and the webhook signature verifier.

For AI assistants, the MCP server in [mcp-server/](mcp-server/README.md)
(`custom-domain-mcp`, released with the SDK) exposes the v1 API as tools:
register a customer's hostname, read its DNS records and checks, recheck or
delete it. It uses the application's API key only.

```python
from custom_domain import Client, CustomDomainMiddleware

client = Client("https://edge.example.net", "cd_...")    # the edge serves /v1 (PUBLIC_API)
domain = client.create_domain("forms.customer.example", reference="ws_8f3a1c")
for record in domain.dns_records:      # show these to the customer
    print(record.type, record.name, record.value)

app.add_middleware(
    CustomDomainMiddleware,
    keys={"1": "<the EDGE_ASSERTION_KEYS secret>"},
    application_id="<application id>",
    workspace_lookup=workspaces.get,   # truthy only for workspaces you serve
)
# request.state.custom_domain.reference is the workspace for this request
```

The middleware also answers the workspace probe the lifecycle worker uses and
lets the origin verification probe through. A complete second SaaS origin is
in [examples/sample_saas/](examples/sample_saas/README.md), a webhook consumer
in [examples/webhook_consumer.py](examples/webhook_consumer.py), and the
BetterCollected integration plan in
[docs/bettercollected-integration.md](docs/bettercollected-integration.md).

Any language can integrate without the SDK: the API contract is in
[docs/api-v1.md](docs/api-v1.md), the assertion format in
[docs/edge-routing.md](docs/edge-routing.md) and the webhook signature in
[docs/webhooks.md](docs/webhooks.md).

## Instructions for SaaS customers

The API returns the exact records for each hostname:

- a TXT record named `_custom-domain-challenge.<hostname>` with the value
  shown, proving ownership;
- a CNAME record for `<hostname>` pointing at the application's edge target.

The domain is checked automatically (and on request through
`POST /v1/domains/{id}/checks`); every failing check carries a stable error
code and a message the application can show to the customer.

## Documentation

- [docs/data-model.md](docs/data-model.md): applications, domains, claims, checks, events and their invariants.
- [docs/api-v1.md](docs/api-v1.md): the v1 API with worked examples, error codes and the migration from the legacy `/domains` endpoint.
- [docs/dns-verification.md](docs/dns-verification.md): ownership and routing checks, diagnostics, status rules.
- [docs/tls-readiness.md](docs/tls-readiness.md): on-demand certificates and the HTTPS readiness probe.
- [docs/edge-routing.md](docs/edge-routing.md): request routing and the signed workspace assertion.
- [docs/lifecycle.md](docs/lifecycle.md): the status machine, the workspace probe and the worker.
- [docs/webhooks.md](docs/webhooks.md): subscriptions, signatures, delivery and replay.
- [docs/deployment.md](docs/deployment.md): production layout, secrets, monitoring, backups and rollback.
- [docs/portal.md](docs/portal.md): the operator portal, its pages, how to reach it and what it protects against.
- [docs/operator-api.md](docs/operator-api.md): the operator actions over HTTP, for control planes and provisioning scripts.
- [docs/hosting-aws.md](docs/hosting-aws.md), [docs/hosting-azure.md](docs/hosting-azure.md), [docs/hosting-gcp.md](docs/hosting-gcp.md), [docs/hosting-hetzner.md](docs/hosting-hetzner.md) and [docs/hosting-digitalocean.md](docs/hosting-digitalocean.md): one-server installs on each provider.
- [docs/bettercollected-integration.md](docs/bettercollected-integration.md): a worked integration of a SaaS product.
- [docs/operations.md](docs/operations.md) and [docs/decisions/0001-certificate-storage.md](docs/decisions/0001-certificate-storage.md): certificate storage, multi-instance coordination, trust boundaries.
- [AGENTS.md](AGENTS.md): how to work on this repository, for people and coding agents alike.

## Development

The project uses [uv](https://docs.astral.sh/uv/); the SDK is a workspace
member. [AGENTS.md](AGENTS.md) has the conventions, the rules the code
depends on, what each kind of change must include, and the release steps.
Coding agents read it automatically (Claude Code through `CLAUDE.md`).

```bash
uv sync                      # install dependencies from uv.lock
uv run pytest                # test suite on a temporary SQLite database
uv run ruff check app tests sdk
uv run custom-domain --help
```

Set `TEST_DATABASE_URL` to a PostgreSQL URL to run the suite against
PostgreSQL. Tests that drive a real edge need the `caddy` binary (2.11) on the
path and are skipped without it; `REQUIRE_CADDY=1` makes them fail instead, as
in CI. `tests/test_e2e_production_like.py` runs the complete flow through
Caddy with on-demand certificates from its internal CA and two SDK-based
origins.

## Legacy deployments

Deployments made before the v1 API still work: the `/domains` endpoint stays
available while `ENABLE_LEGACY_API=true`. Import the existing hostnames with
`custom-domain legacy import`, then set `ENABLE_LEGACY_API=false` so the edge
configuration is derived from the database. The `https_data` volume keeps the
certificate store across the change.

## Source code

[https://github.com/sireto/custom-domain](https://github.com/sireto/custom-domain)

## Support

Questions, or help with a deployment: info@sireto.com
