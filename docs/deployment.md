# Production deployment

Status: implemented for issue #12. Companion to [operations.md](operations.md)
(reconciliation, backup, restore) and
[ADR 0001](decisions/0001-certificate-storage.md).

## Topology

`deploy/compose.production.yml` runs five services from the one image:

| Service | `CONTAINER_ROLE` | Runs | Reaches |
| --- | --- | --- | --- |
| `db` | | PostgreSQL 16 with a persistent volume and health check | |
| `redis` | | Certificate store for the edges (AOF persistence, AUTH) | |
| `api` | `api` | migrations, then the management API; no in-process workers | db, edge gateway |
| `worker` | `worker` | lifecycle checks, edge reconciliation, webhook delivery | db, edge gateway |
| `edge` | `edge` | Caddy (as `caddy`) and the configuration gateway (as `app`) | api, redis |

Networks: `backend` (db, api, worker), `control` (api, worker, edge) and
`edge_storage` (edge, redis). The edge publishes 80 and 443 only; the
management API is published on the host's loopback only, and applications
reach its `/v1` endpoints through the edge when `PUBLIC_API=true` (see
below). `EDGE_ASK_TRUSTED_HOSTS` is the `control`
subnet so only edge containers can call the internal endpoints, and
`EDGE_TOKEN` is additionally required on the assert, origins and metrics
calls.

### The configuration gateway

Caddy's admin API in the edge container listens on `127.0.0.1:2020` and is
reachable by nothing outside that container. The gateway
(`custom-domain edge gateway`) listens on `:2019` and is what the reconciler
talks to (`CADDY_ADMIN_URL=http://edge:2019`). It exposes only
`GET /config/`, `GET /config/apps` (the `apps` subtree, never `admin` or
`storage`), `GET /metrics` (each edge hostname's request and response byte
totals, and nothing else of Caddy's metrics) and `POST /config/apps`, and it accepts a `POST` only when the
payload is exactly the reconciler's shape: the health and 404 routes, and
per application a route whose handlers are the header strip, the assert
subrequest to the configured upstream, and a `reverse_proxy` that dials an
address the management API currently lists for a verified active origin and
presents that origin's own name for TLS (`GET /internal/edge/origins`
returns the resolved addresses paired with the name), with only the
documented headers. `file_server`, extra listeners, extra servers, storage or TLS
policy changes and `/load` are refused. This closes the boundary described
in [operations.md](operations.md#private-key-boundaries): a compromised API
or worker can no longer make Caddy serve the certificate store.

The API is started with uvicorn's proxy-header handling disabled
(`--no-proxy-headers`): the internal endpoints authorize the edge by the
address that connects to them, and Caddy adds `X-Forwarded-For` with the
browser's address to every assert subrequest, which would otherwise be taken
for the client. A reverse proxy in front of the management API therefore
appears under its own address in the API's logs.

## Installing on a fresh server

`deploy/install.sh` performs the layout below on a fresh Ubuntu 22.04/24.04
or Debian 12 host: Docker Engine, `/opt/custom-domain` with the Compose file
and a generated `.env` (fresh secrets, the image pinned by
`CUSTOM_DOMAIN_VERSION`), ufw with SSH, 80 and 443 open, the stack started,
and a `custom-domain` command on the host that runs the operator CLI in the
API container. `deploy/cloud-init.yaml` wraps it as user data for cloud
servers; see [hosting-hetzner.md](hosting-hetzner.md) and
[hosting-digitalocean.md](hosting-digitalocean.md). After installation,
`custom-domain doctor` reports the state of the deployment: database and
migrations, edge gateway, certificate authority, reconciler, applications
and whether their CNAME targets reach this edge.

Outbound requests the installer makes: Docker's package repository and
registry, `raw.githubusercontent.com` (the installer and the Compose file at
the release) and `ghcr.io` (the image). When the server's own IPv4 address
is private (behind 1:1 NAT, as with an AWS Elastic IP), it also asks
`checkip.amazonaws.com` for the public address, only to print the A record
in its summary; nothing is configured from the answer.

## Releasing

A release is one version number, for example `0.3.1`, used everywhere: the
git tag (no prefix), the image tag `ghcr.io/sireto/custom-domain:0.3.1`
(also `0.3`), the SDK `custom-domain-sdk==0.3.1` and the MCP server
`custom-domain-mcp==0.3.1` on PyPI, and the TypeScript SDK
`@customdomainapi/sdk@0.3.1` on npm. Developers
run the SDK at the version of the service they integrate with, and
upgrading is one number. To release:

1. Set `version` in `pyproject.toml`, `sdk/pyproject.toml`,
   `mcp-server/pyproject.toml` and `sdk/typescript/package.json`, `VERSION` in
   `sdk/typescript/src/version.ts`, and both versions in `mcp-server/server.json`,
   to the same value (then `npm install --package-lock-only` in `sdk/typescript/`
   so its lockfile names it too; `tests/test_mcp_server.py` fails until they
   all agree). Leave the cloud templates alone: they name the latest
   *released* version (step 4). Refresh `uv.lock` and merge that through a
   pull request.
2. Tag the merge commit with the bare version and push the tag:
   `git tag 0.3.1 <merge commit> && git push origin 0.3.1`.
3. The tag runs the publish workflows. Each first checks that the tag
   equals the versions it publishes and fails otherwise; the image
   workflow then publishes the image, and the SDK workflow both packages
   (Trusted Publishing, see below) and the MCP Registry listing. The
   TypeScript SDK workflow publishes to npm once it is enabled (see below).
   The image workflow's run summary shows the pushed digest; put it in the
   release notes so operators can pin it
   ([Pinning the image by digest](#pinning-the-image-by-digest)).
4. Once the image is published, move the cloud templates to the release in
   a second, small pull request: the version in `deploy/LATEST_RELEASE`,
   `CUSTOM_DOMAIN_VERSION` in `deploy/cloud-init.yaml`, `Version` in
   `deploy/aws/custom-domain.yaml`, `version` in `deploy/azure/main.bicep`
   (then rebuild `deploy/azure/azuredeploy.json` with
   `az bicep build --file deploy/azure/main.bicep --outfile deploy/azure/azuredeploy.json`)
   and the default in `deploy/gcp/deploy.sh`, plus the version in the
   pre-0.4.0 upgrade commands under [Upgrading](#upgrading).
   `tests/test_cloud_templates.py` fails until every template names the
   version in `deploy/LATEST_RELEASE`, and fails if that version is newer
   than `pyproject.toml`'s (or, in a clone with tags, is not a tag).

`deploy/cloud-init.yaml` fetches the installer and the Compose file at the
tag named by `CUSTOM_DOMAIN_VERSION`, so a user-data document only ever
runs the release it names. The AWS, Azure and Google Cloud templates do the
same. The Azure and Google Cloud buttons load their files from `main`. On
`main`, `pyproject.toml` names the next release as soon as its pull request
merges, before the tag and the image exist, so the template defaults follow
`deploy/LATEST_RELEASE` instead and a button never installs a release that
is not published yet. The AWS Launch Stack link opens the copy the
publishing workflow uploads from the release tag (below); that copy's
default is set to the tag itself.

### Cloud template publishing setup

CloudFormation opens templates only from S3, so the AWS Launch Stack link
needs the template there. `.github/workflows/publish-templates.yml` uploads
it on every release tag to `s3://<bucket>/custom-domain/<version>/` and
`.../latest/` once two repository variables are set (it does nothing until
then). The uploaded copy's `Version` default is set to the tag, since the
template on `main` still names the previous release at that moment:

- `AWS_TEMPLATES_BUCKET`: a bucket whose objects under `custom-domain/` are
  publicly readable (a bucket policy granting `s3:GetObject` on
  `arn:aws:s3:::<bucket>/custom-domain/*`).
- `AWS_TEMPLATES_ROLE_ARN`: an IAM role that trusts GitHub's OIDC provider
  (`token.actions.githubusercontent.com`) for `repo:sireto/custom-domain:ref:refs/tags/*`
  and may `s3:PutObject` on the same prefix. No AWS keys are stored.

The Launch Stack link in [hosting-aws.md](hosting-aws.md) then points at
`https://<bucket>.s3.amazonaws.com/custom-domain/latest/custom-domain.yaml`.

### SDK publishing setup

`custom-domain-sdk` is published by `.github/workflows/publish-sdk.yml` with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): PyPI
accepts a short-lived OpenID Connect token that GitHub mints for that
workflow, so no PyPI API token exists anywhere. The publisher registered on
PyPI is: project `custom-domain-sdk`, owner `sireto`, repository
`custom-domain`, workflow `publish-sdk.yml`, environment `pypi`. The `pypi`
environment exists in the repository settings; restricting it to protected
tags or to the releasing maintainers limits who can mint the token. A
manual run of the workflow ("Run workflow", target `testpypi`) rehearses a
release on test.pypi.org (same publisher there, environment `testpypi`)
and never publishes to PyPI.

`custom-domain-mcp` has its own publisher with the same settings (project
`custom-domain-mcp`) and its own jobs, so a failure there never stops the
SDK. After it is on PyPI, the `publish-mcp-registry` job lists the version
on the [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.sireto/custom-domain`, from `mcp-server/server.json`. It logs in
with GitHub's OpenID Connect token, which the registry accepts for the
`io.github.sireto/` namespace, so no token is stored. The registry accepts
the listing only if the package's README on PyPI contains
`<!-- mcp-name: io.github.sireto/custom-domain -->`. The job pins
`mcp-publisher` to a release and checks its SHA-256; to update it, change
both values from the registry's release checksums file.

### TypeScript SDK publishing setup

`@customdomainapi/sdk` (`sdk/typescript/`) is published by
`.github/workflows/publish-sdk-ts.yml` with npm's
[trusted publishing](https://docs.npmjs.com/trusted-publishers): npm
accepts a short-lived OpenID Connect token that GitHub mints for that
workflow and records a provenance statement, so no npm token is stored. The
workflow is **off** until the repository variable `NPM_PUBLISH_ENABLED` is
`true`; until then every release tag skips it. The one-time setup:

1. On npmjs.com, create the organization `customdomainapi` (free plan,
   public packages), so the `@customdomainapi` scope exists, and add the
   maintainers who may publish. Turn on two-factor authentication for them.
2. Publish the first version by hand, because npm only lets a trusted
   publisher be configured on a package that already exists. From a clean
   checkout of the release tag:
   `cd sdk/typescript && npm ci && npm test && npm run build && npm publish --access public --provenance=false`
   (provenance needs CI, so the first upload goes without it).
3. On the package's page, Settings, Trusted publishing, add a GitHub Actions
   publisher: organization `sireto` (the repository's **GitHub** owner, not
   the npm organization), repository `custom-domain`, workflow
   `publish-sdk-ts.yml`, environment `npm`. Then, under Publishing access,
   choose "Require two-factor authentication and disallow tokens", so only
   the workflow (and maintainers with 2FA) can publish.
4. In the GitHub repository settings, create the environment `npm`
   (restrict it to tags, or to the releasing maintainers), and set the
   repository variable `NPM_PUBLISH_ENABLED` to `true`.
5. Rehearse with a manual run of the workflow ("Run workflow"): it builds,
   tests and runs `npm publish --dry-run`. From then on, each release tag
   publishes the package with provenance, after checking that the tag equals
   `sdk/typescript/package.json`.

## Upgrading

`custom-domain upgrade <version>` on the host re-runs the installer from
that release. It refreshes the Compose file, adds any settings the release
introduced to `.env` (with generated values where they are secrets), moves
`CUSTOM_DOMAIN_IMAGE` to the version, then pulls and restarts the stack.
Existing data and secrets are never touched. Values given on the command
line or in the environment win over `/etc/custom-domain-install.env`, so
the version cloud-init wrote at creation does not hold an upgrade back.
`custom-domain upgrade <version> --digest sha256:...` also pins the image
by digest ([below](#pinning-the-image-by-digest)).

The Compose file is part of a release (a published port, a setting the
three containers must share), so the installer treats it carefully. It
keeps two checksums beside the file: the local file as it last wrote or
accepted it, and the release file that local copy corresponds to.

- **Unmodified since the installer wrote it, and still the pristine release
  file:** replaced by the new release's file; the upgrade proceeds.
- **Modified locally, or installed by hand before the installer existed:**
  the upgrade **stops before changing anything**, exits with status 3, and
  writes the release's file next to yours as `compose.production.yml.new`.
  Merge it into `compose.production.yml` (keeping your changes), then run
  `custom-domain upgrade <version> --accept-compose`: the merged file is
  recorded as the installed one and the upgrade continues.
- **An accepted (customized) file** is never overwritten. A later release
  that does not change the Compose file keeps it as it is; a release that
  does change it stops for another merge, the same way, so upstream changes
  are always seen and local changes are never silently discarded.

Upgrading by editing the image tag alone is not enough and is no longer
documented: running the new image with an old Compose file is exactly the
failure the installer's stop prevents.

Installations made before 0.4.0 have a host command without `upgrade`
(`custom-domain upgrade` then fails with the CLI's `invalid choice`). Run the
installer directly once, which rewrites the host command:

```
curl -fsSL https://raw.githubusercontent.com/sireto/custom-domain/0.12.0/deploy/install.sh -o /root/custom-domain-install.sh
CUSTOM_DOMAIN_VERSION=0.12.0 bash /root/custom-domain-install.sh
```

Such an installation has no recorded Compose checksum, so unless its
Compose file already equals the release's, this first run stops for the
merge described above; after merging, continue with
`CUSTOM_DOMAIN_ACCEPT_COMPOSE=1` in front of the same command. Later
upgrades are `custom-domain upgrade <version>`.

## Images

The service image is published to GitHub Container Registry by the
`Docker image` workflow (`.github/workflows/docker-image.yml`), authenticated
with the workflow's own token: `ghcr.io/sireto/custom-domain:latest` and
`sha-<commit>` for every push to `main`, and `<version>` plus
`<major>.<minor>` for a release tag `<version>` (a bare version number,
which also releases the SDK at that version). Pin a deployment to a
version or a `sha-` tag through `CUSTOM_DOMAIN_IMAGE` rather than following
`latest`, and by digest for a reference that cannot move (below). Each published image carries a build provenance attestation;
`gh attestation verify oci://ghcr.io/sireto/custom-domain:<tag> --owner sireto`
checks that it was built by this repository's workflow.

The package must be public for hosts to pull it without a token (once, in
the package's settings on GitHub: change visibility to public, and link it
to this repository if the `org.opencontainers.image.source` label has not
done so). Compose does not re-pull an existing tag: run
`docker compose -f compose.production.yml pull` before `up` to upgrade.

### Pinning the image by digest

A tag can move; a digest cannot. The `Docker image` workflow writes the
digest it pushed to the run's summary (`ghcr.io/sireto/custom-domain@sha256:...`,
with the matching `custom-domain upgrade <version> --digest sha256:...`
line on a release tag), so the release notes can carry it. Anyone can also
read it from the registry:
`docker buildx imagetools inspect ghcr.io/sireto/custom-domain:<version>`.

Give the installer that digest with the version and it pins the image by
content:

```
CUSTOM_DOMAIN_VERSION=0.12.0 CUSTOM_DOMAIN_IMAGE_DIGEST=sha256:<64 hex digits> bash install.sh
custom-domain upgrade 0.13.0 --digest sha256:<64 hex digits>
```

`CUSTOM_DOMAIN_IMAGE_DIGEST` (also accepted in
`/etc/custom-domain-install.env`) must be exactly `sha256:` and 64 lowercase
hex digits, and needs a version other than `latest`; anything else stops the
installer before it changes anything. `.env` then holds
`CUSTOM_DOMAIN_IMAGE=ghcr.io/sireto/custom-domain:<version>@sha256:<digest>`.
Compose and `docker pull` fetch that reference by its digest (the tag only
names the release for people reading it), and a digest the registry does
not have fails the pull with `manifest unknown` instead of running
something else.

A pin belongs to one release:

- **The same version again** (`custom-domain upgrade` with no version, or
  with the installed one) keeps the pin.
- **Another version without a digest** drops the pin, with a warning that
  names the dropped digest: the old digest would run the old image under
  the new tag. Give `--digest` to pin the new release.
- **A digest in `/etc/custom-domain-install.env`** applies only with the
  version in the same file, so the digest cloud-init wrote at creation never
  pins the version given to `custom-domain upgrade`.

The option needs an installer that knows it: `custom-domain upgrade` runs the
installer of the release it moves to, so `--digest` works when that release
has it (from the release after 0.12.0).

## The edge's own name

`EDGE_HOSTNAME` (for example `edge.example.net`) is the name the edge is
reached at: the default `--cname-target` for applications, a name the edge
obtains a certificate for and answers the health path on from the first
start, and where the portal is served. Point it at the server's addresses
before or right after installing. Without it the edge has no name of its
own until the first application exists, so the portal is reachable only
over the tunnel and `doctor` says so.

## The operator portal

Every action of the `custom-domain` command is also a page under `/portal`
of the management API, once `PORTAL_PASSWORD` is set (the installer
generates one into `deploy/.env`). With `PORTAL_ALLOWED_IPS` set, the edge
serves it at `https://<edge name>/portal` for those addresses only (the
installer fills in the address you install from); the API is also
published on `127.0.0.1:9000` of the host, so an SSH tunnel
(`ssh -N -L 9000:127.0.0.1:9000 root@<server>`, then
http://localhost:9000/portal) always works. See [portal.md](portal.md).

## The v1 API through the edge

With `PUBLIC_API=true` (the installer's default for new installations), the
edge serves the v1 API at `https://<edge name>/v1` on each of its own names,
so an application's backend anywhere on the internet calls
`https://edge.example.net/v1/...` with its credential and needs no reverse
proxy of your own. The same route serves the customers' setup pages,
`https://<edge name>/connect/<token>` ([api-v1.md](api-v1.md#setup-page)); each
needs the token of a connect session. Nothing else of the management API is routed: `/internal`
(the edge-only endpoints), `/portal` (which has its own allowlisted route),
`/metrics` and the legacy `/domains` API stay unreachable from outside, and
customer hostnames never expose the API. Every v1 call requires an
application credential, and the edge strips its own `X-Custom-Domain-*`
headers from these requests. As with the portal, the configuration gateway
accepts the route only in the reconciler's exact shape, on the edge names,
and only while the API states that it is public; the edge container needs
no copy of the setting.

`custom-domain doctor` reports where the API is served. With
`PUBLIC_API=false` it listens on `127.0.0.1:9000` of the host only; put an
authenticated reverse proxy in front if applications must reach it. Forward
only `/v1` there: the API trusts its private peers, so a proxy that forwards
every path would also put `/portal` and the operator API (`/operator`) on the
internet with only their password or token in the way, and without the
allowlists and throttling the edge gives them. Existing
installations keep their setting on upgrade: add `PUBLIC_API=true` to
`deploy/.env` (or run `PUBLIC_API=true custom-domain upgrade <version>`) and
restart the api and worker to turn it on.

## Secrets

Copy `deploy/env.production.example` to `deploy/.env` and fill it. Compose
reads `deploy/.env` on its own for the `${...}` substitutions in the file,
and the api and worker load all of it through `env_file`, so no variable has
to be exported in the shell; run every command from the `deploy` directory
(or pass `--project-directory deploy`). Do not export these settings in the
shell either: Compose prefers a shell value over `.env` for a substitution
but not for `env_file`, so the edge would see a different value than the
reconciler. The installer clears them before it runs Compose.

It holds the database password, `EDGE_ASSERTION_KEYS` (signs assertions),
`EDGE_TOKEN` (edge to API), `SERVICE_SECRET_KEY` (encrypts secrets the
service stores, such as the Domain Connect signer's credential, and signs
the Domain Connect return state; the installer generates it, and changing it
means setting the signer again), `CADDY_REDIS_PASSWORD` and
`CADDY_REDIS_ENCRYPTION_KEY`. With `DOMAIN_CONNECT_SIGNER=local` it also
holds the Domain Connect private key ([domain-connect.md](domain-connect.md)). Only the edge containers need the Redis
values; the entrypoint scrubs them from the Python processes' environment,
and Caddy itself starts with an empty environment.
Application credentials and webhook secrets are issued by the CLI and shown
once. Logs never carry any of these: the code does not log them, and a
redaction filter masks credential-looking values as a last line of defence.

The internet-facing `edge` container has no `env_file`. It gets only what
Caddy and the gateway read, each taken from `.env` with the default the code
uses when the setting is unset: `EDGE_TOKEN`, `DISABLE_HTTPS`, `ACME_EMAIL`,
`EDGE_TLS_ISSUER`, `EDGE_HTTPS_PORT`, `EDGE_HTTP_PORT`, the Redis certificate
store settings (`CADDY_REDIS_PASSWORD`, `CADDY_REDIS_ENCRYPTION_KEY`,
`CADDY_REDIS_DB`, `CADDY_REDIS_KEY_PREFIX`) and `LOG_LEVEL`, plus fixed values
in the Compose file (`EDGE_ASK_URL`, `EDGE_ASSERT_UPSTREAM`, `API_URL`, and
`EDGE_RECONCILE_ENABLED=false`, since the edge never reconciles and so needs no
assertion keys). The database URL and password, `SERVICE_SECRET_KEY`,
`OPERATOR_API_TOKEN`, `PORTAL_PASSWORD`, `EDGE_ASSERTION_KEYS` and the
`DOMAIN_CONNECT_*` settings never reach it. The portal allowlist, the operator
allowlist and `PUBLIC_API` reach the gateway from the API
(`/internal/edge/origins`), so changing them needs no edge restart.
`tests/test_deploy_files.py` checks that, for any `.env`, the gateway accepts
exactly what the api and worker build. A Compose file you customized keeps
`env_file` on the edge until you merge the release's `compose.production.yml.new`.
The api and worker still share all of `.env`. The worker needs the database,
the assertion keys and the operator token's presence for the reconciler, and
its DNS checks reach the Domain Connect code, so it is not narrowed further.

## Bring-up

```
cd deploy
docker compose -f compose.production.yml config >/dev/null   # verifies the env file is complete
docker compose -f compose.production.yml up -d db redis
docker compose -f compose.production.yml up -d api          # runs migrations
docker compose -f compose.production.yml up -d worker edge
docker compose -f compose.production.yml exec api custom-domain application create --slug acme --name "Acme" --cname-target acme.edge.example.net
docker compose -f compose.production.yml exec api custom-domain origin register --application acme --host app.acme.example
docker compose -f compose.production.yml exec api custom-domain origin verify --application acme --host app.acme.example --activate
docker compose -f compose.production.yml exec api custom-domain credential issue --application acme --label backend
```

Health checks: the API answers `/v1/openapi.json`, the gateway answers
`/config/apps`, PostgreSQL `pg_isready`, Redis `PING`. Compose starts
dependants only when these pass.

## Monitoring

`GET /internal/metrics` (trusted networks plus `EDGE_TOKEN`) exposes
Prometheus metrics:

| Metric | Covers |
| --- | --- |
| `custom_domain_dns_check_seconds` | DNS verification latency per domain |
| `custom_domain_checks_total{check,status,error_code}` | Ownership, routing, certificate and origin outcomes, including issuance failures (`tls_handshake_failed`), renewal health (`certificate_expiring`, `certificate_expired`) and routing problems (`edge_not_reached`, `workspace_mismatch`) |
| `custom_domain_status_transitions_total{to,reason}` | Domains becoming ready, needing attention, suspended |
| `custom_domain_domains{status}` | Live domains by status (gauge) |
| `custom_domain_tls_ask_total{decision}` | Certificate authorizations allowed, denied, untrusted |
| `custom_domain_edge_assert_total{decision}` | Routing decisions; `denied` are routing errors reaching the edge |
| `custom_domain_webhook_deliveries_total{outcome}` | delivered, retry, abandoned |
| `custom_domain_reconcile_total{outcome}` | applied, unchanged, `config_rejected`, `caddy_unavailable` |
| `custom_domain_registrations_total{outcome}` | created and each refusal code |

Caddy's own logs (JSON on the edge container's stdout) carry ACME details;
`tls.obtain` and `tls.renew` entries name the hostname and the CA error.

## Rate limits and abuse controls

| Surface | Control |
| --- | --- |
| Registrations | 120 per application per hour (`REGISTRATION_MAX_PER_WINDOW`), plus idempotency keys against duplicates |
| Manual rechecks | 1 per domain per minute, 60 per application per hour |
| Domain Connect links | 10 per domain and 120 per application per hour; the return only makes DNS checks due, at most once per worker run |
| DNS checks | Worker driven with bounded backoff; never triggered by unauthenticated traffic |
| Certificate issuance | Only for authorized hostnames (verified claim, active application); Caddy asks per hostname and caches decisions per handshake; unknown SNI never reaches the CA |
| Origin traffic | Only `ready` hostnames route, every request is re-checked at the assert step; origins should sit behind network rules that admit only the edge |
| Names | Canonicalized; wildcards, IP literals, public suffixes, apex names (public suffix list), reserved zones (`localhost`, `.local`, `.internal`, `.arpa`, `.onion`, `.invalid`, `.home`, `.lan`, `.corp`) and malformed labels are refused with stable codes |
| Internal endpoints | Trusted networks plus `EDGE_TOKEN`; the management port is never public |

Per-IP request limiting at the edge (for example against a flood on one
hostname) is not built in; put a rate limiting layer or Caddy's rate limit
plugin in front if a deployment needs it.

## Capacity and CA rate limits

- Each customer hostname is usually its own registered domain, so Let's
  Encrypt's 50 certificates per registered domain per week rarely binds; the
  limits that do are 300 new orders per account per 3 hours and 5 failed
  validations per hostname per hour. A large import therefore obtains
  certificates over hours, driven by readiness probes and their backoff, not
  all at once; plan cutovers of thousands of hostnames in batches and watch
  `custom_domain_checks_total{check="certificate",error_code="tls_handshake_failed"}`.
- Renewals happen at two thirds of the lifetime and count toward the same
  order limit; a fleet of 10,000 hostnames renews about 170 per day.
- One edge container comfortably serves thousands of hostnames; certificate
  count, not traffic, dominates memory. Add edges behind a load balancer
  sharing the same Redis; they issue each certificate once.
- The worker checks each domain every 6 hours when healthy; with 10,000
  domains that is about 30 DNS lookups per minute plus probes.
- The assert subrequest adds one loopback-network HTTP call and one indexed
  query per proxied request; a single API replica sustains thousands of
  requests per second of that. Add API replicas behind the `control`
  network if edge traffic grows.

## Backups and restore

Follow [operations.md](operations.md#backup): nightly `pg_dump` and Redis
snapshots, both treated as secrets. Restore the database first, then the
certificate store, then start `api`, `worker` and `edge`.

## Rollback

Every release keeps the database migrations backward compatible for one
release, so rolling back is:

1. `docker compose -f compose.production.yml pull` the previous image tag
   and `up -d api worker edge`; the reconciler rebuilds Caddy's configuration
   from the database on start.
2. If a migration must be reverted, `custom-domain db downgrade --revision
   <previous>` before starting the old image, using the pre-upgrade
   `pg_dump` as the safety net.
3. For deployments still on the legacy `/domains` API, rollback means
   `ENABLE_LEGACY_API=true` with the single-container layout; the legacy
   config in the `https_domains` volume is untouched by the new path.

Domains keep serving during a rollback: certificates stay in Redis and the
edge keeps its last configuration until a reconciler applies a new one.

## Verified by tests

`tests/test_hardening.py` and the earlier suites cover: restart and
convergence of the reconciler, two reconciler instances with a deletion in
between, a rejected or unreachable Caddy leaving state intact, failed DNS
with backoff, deletion racing the checks, hostname reassignment with fresh
tokens, the gateway refusing every non-reconciler shape, trusted networks
and tokens, registration limits, metrics and log redaction.
