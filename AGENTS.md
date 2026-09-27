# Instructions for coding agents

Read this before changing anything in the repository. It covers how to work
here, which rules the code depends on, and what a change must include. The
[README](README.md) explains what the service does; `docs/` has the details.

## The service in one paragraph

Custom Domain lets several SaaS applications serve their customers' own
hostnames. An application registers a hostname through the v1 API and hands
the customer two DNS records (a TXT record proving ownership and a CNAME to
the edge). Workers check DNS, the edge (Caddy) obtains a certificate on
demand, and the hostname goes live only when four checks pass: ownership,
routing, certificate and origin. Every proxied request carries a signed
assertion naming the application, domain and workspace, so an application
never selects a tenant from the `Host` header. Operators manage everything
with the `custom-domain` command or the equivalent portal at `/portal`.

## Commands

Use **uv** for everything. Never use system Python, bare `pip` or a
hand-made virtualenv.

```bash
uv sync --all-packages                 # install the service and the SDK (workspace member)
uv run pytest                          # full suite on a temporary SQLite database
uv run ruff check app tests sdk        # lint; CI runs `ruff check app tests`
uv run ruff format <files you changed> # format only what you touched
uv run custom-domain --help            # the operator CLI
```

Run the suite against PostgreSQL before finishing any change that touches
models, queries or migrations:

```bash
docker run -d --name cd-test-pg -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=test -p 55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql+psycopg://test:test@localhost:55432/test uv run pytest
```

- **Check the exit code, not the output.** pytest runs with `-q`, so a
  passing run prints dots and no "passed" line.
- **Caddy tests** need the `caddy` binary (2.11) on the path and are skipped
  without it. CI sets `REQUIRE_CADDY=1`, which makes them fail instead.
- **Do not run `ruff format` over the whole tree.** `app/caddy/`,
  `app/security.py` and `main.py` are legacy prototype code kept for the
  `/domains` API. They are not restyled; reformatting them makes unrelated
  diffs.

## Where things are

| Path | What it holds |
|---|---|
| `app/models/` | SQLAlchemy models; `enums.py` has every status |
| `app/services/` | All business rules. The API, CLI, portal and workers call these and nothing else writes state |
| `app/services/errors.py` | `ServiceError` subclasses with stable `code`s; the API maps them to responses |
| `app/v1/` | The v1 API (`router.py`), its schemas and examples, the webhook API, and `internal.py` (edge-only endpoints: TLS ask, assert, origins) |
| `app/edge/` | Caddy configuration (`config.py`), the validating gateway (`gateway.py`), the reconciler, the assertion signer, the HTTPS probe |
| `app/dns/`, `app/webhooks/` | The DNS checks worker and the webhook delivery worker |
| `app/operator/` | The operator API (`/operator/v1`): applications, origins and credentials over HTTP, behind `OPERATOR_API_TOKEN` |
| `app/portal/` | Operator portal: `views.py` (routes), `presenters.py` (what every status means, in words), `auth.py`, Jinja templates |
| `app/cli.py` | The `custom-domain` command |
| `app/db/migrations/versions/` | Alembic migrations, numbered `0001`, `0002`, ... |
| `sdk/custom_domain/` | The Python SDK: client, assertion verifier, ASGI middleware, webhook verifier |
| `deploy/` | Production Compose file, installer (`install.sh`), cloud-init, local Compose, and the AWS, Azure and Google Cloud templates |
| `tests/` | One file per area; `conftest.py` has the database fixtures |
| `docs/` | Contracts and runbooks; `docs/openapi.json` is generated |

## Rules the code depends on

Breaking one of these is a security or correctness bug, even when the tests
still pass.

- **Tenancy comes from the assertion, never from `Host`.** The edge signs
  `X-Custom-Domain-Assertion` and strips any client-supplied copy. Origins
  select the workspace from the verified assertion
  ([docs/edge-routing.md](docs/edge-routing.md)).
- **Everything is scoped to one application.** Every service call takes the
  application. A domain, origin, credential or webhook that belongs to
  another application is reported as not found, never as forbidden.
- **A hostname is served only through `is_serveable`.** It requires an
  active application, a live `ready` domain, a verified claim and all four
  checks passing, and it is evaluated on every call. Do not add another
  path that authorizes a certificate or a route.
- **Rows are never reused, and history is retained.** Deleting a domain
  leaves a tombstone for 90 days; deleting an application archives it with
  its tombstones. Only `purge_tombstones` removes rows. Re-claiming a
  hostname creates a new row with a new token
  ([docs/data-model.md](docs/data-model.md)).
- **Only the reconciler writes the edge configuration, and the gateway
  checks it.** `app/edge/gateway.py` accepts only the exact shape
  `app/edge/config.py` builds. If you change `build_apps`, change
  `validate_apps` and its tests in the same change, or every reconcile is
  rejected in production.
- **The api, worker and edge containers must agree on gateway-checked
  settings.** In `deploy/compose.production.yml` these are `EDGE_ASK_URL`,
  `EDGE_ASSERT_UPSTREAM` and the other `EDGE_*`, `ACME_*` and
  `DISABLE_HTTPS` values. `tests/test_deploy_files.py` enforces it; a
  mismatch once disabled TLS on a live deployment.
- **Outbound requests go to public addresses only.** Origin verification
  and webhook delivery refuse private, loopback and metadata addresses
  unless `ORIGIN_ALLOW_PRIVATE=true`, and delivery re-resolves on every
  attempt.
- **Secrets are shown once.** API keys and webhook signing secrets appear
  only in the response that created them. Only hashes (credentials) or the
  secret for signing (webhooks) are stored. Never log them.
- **The portal is the CLI in a browser.** Every portal action calls the same
  service function as the CLI. Every POST checks the session's CSRF token.
  Pages use no JavaScript, under a strict Content-Security-Policy.
  Post-action notices are fixed messages picked by a code, never text from
  the URL.

## What a change must include

**A new or changed operator action:** the service function with a
`ServiceError` for each refusal (mapped to a status in `app/v1/errors.py`),
the CLI command, the portal route and template, the operator API endpoint
(`app/operator/api.py`) when a program would need it, the rows in
[docs/portal.md](docs/portal.md) and [docs/operator-api.md](docs/operator-api.md),
and tests for each. Then regenerate `docs/openapi.json`.

**A new status, check or error code:** the enum or error class, its
plain-language entry in `app/portal/presenters.py`, the documentation in
[docs/lifecycle.md](docs/lifecycle.md) or [docs/api-v1.md](docs/api-v1.md),
and the OpenAPI export below if the API exposes it.

**A change to the v1 API:** regenerate the committed contract and run its
tests:

```bash
uv run custom-domain openapi export --output docs/openapi.json
uv run pytest tests/test_openapi.py tests/test_v1_api.py
```

**A migration:**
- Add the next numbered file under `app/db/migrations/versions/`.
- Use `op.batch_alter_table` so SQLite works.
- Keep it backward compatible for one release: add nullable columns, and
  drop nothing that the previous release still reads.
- Write `downgrade()`.
- Round-trip it on both databases with `custom-domain db upgrade`, then
  `db downgrade`, then `db upgrade`.
- The api container applies migrations when it starts.

**A change to `deploy/`:** keep `tests/test_deploy_files.py`,
`tests/test_install_script.py` and `tests/test_cloud_templates.py` passing.
The cloud templates (`deploy/aws`, `deploy/azure`, `deploy/gcp`) all write
the same installer settings as `deploy/cloud-init.yaml`; change them
together. Edit `main.bicep`, never `azuredeploy.json`, and rebuild the JSON.
Lint the AWS template with `uvx cfn-lint deploy/aws/custom-domain.yaml`. The installer is also the upgrade
path (`custom-domain upgrade <version>`). It must never overwrite a Compose
file the operator changed; it stops and writes `compose.production.yml.new`
instead.

**Documentation:** update the doc that describes the behaviour in the same
change. The README lists every doc.

## Tests

- **Database fixtures.** `session` gives a session and empties every table
  afterwards. Use it, or a fixture built on it, whenever a test writes to
  the database, otherwise rows leak into the next test.
- **Portal tests.** The `portal` fixture (in `conftest.py`) is a signed-out
  portal client. `sign_in` and `csrf_of` in `tests/test_portal.py` complete
  it. DNS and HTTPS probes are stubbed through `app.state.portal_resolve` and
  `app.state.portal_probe`.
- **No real network in tests.** Origin names are pinned to fixed addresses
  (`pinned_origin_addresses` in `conftest.py`), and DNS goes through fakes.
- **Test addresses.** The documentation ranges (`192.0.2.0/24`,
  `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`) count as non-public
  in Python's `ipaddress`. A test that needs a public address uses a real
  one, such as `93.184.216.34`.

## Releases

One bare version number (no `v` prefix) releases the image and the SDK
together:

1. On a branch, set `version` in `pyproject.toml` and `sdk/pyproject.toml`,
   and the default version of every cloud template (`deploy/cloud-init.yaml`,
   `deploy/aws/custom-domain.yaml`, `deploy/azure/main.bicep` then rebuild
   `azuredeploy.json`, `deploy/gcp/deploy.sh`). Run `uv lock` and open a PR.
   `tests/test_cloud_templates.py` fails until every template names the
   release.
2. After it merges, tag the merge commit (`git tag -a X.Y.Z <sha>`), push
   the tag, then run `gh release create X.Y.Z --verify-tag`.
3. The workflows publish `ghcr.io/sireto/custom-domain:X.Y.Z` and
   `custom-domain-sdk` X.Y.Z on PyPI. They fail if the tag and the two
   versions differ.

Tag and publish only when asked; a release reaches every self-hosted
deployment through `custom-domain upgrade`.

## Git and pull requests

- Branch from `main`, one change per pull request, and describe what changed
  and how it was verified.
- **No LLM or tool attribution** anywhere: not in code, comments, commit
  messages or PR descriptions (no `Co-Authored-By` or "Generated with"
  lines). This is company policy.
- Never commit secrets. `.env`, `.env.*` and `deploy/.env` are ignored; keep
  it that way.
- Keep security in mind in every change, starting with the rules above.

## Integrating a product with the service

If your task is in a SaaS product's repository rather than this one, you
need these instead:

- [sdk/README.md](sdk/README.md): the client, the middleware and the
  webhook verifier.
- [docs/api-v1.md](docs/api-v1.md): the API with examples and error codes.
- [docs/edge-routing.md](docs/edge-routing.md): the assertion format.
- [docs/webhooks.md](docs/webhooks.md): the webhook signatures.
- [examples/sample_saas/](examples/sample_saas/README.md): a complete origin.
- [docs/bettercollected-integration.md](docs/bettercollected-integration.md):
  a worked integration plan.
