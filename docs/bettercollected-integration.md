# BetterCollected integration

Status: issue #13. This repository provides everything the integration
needs and proves it end to end with a BetterCollected-like origin and a
second application; the changes inside BetterCollected itself are specified
here and made in its own repository.

## What BetterCollected does today

- A workspace stores `custom_domain`, `custom_domain_verified` and
  `custom_domain_disabled`. Setting a domain writes it to the workspace,
  checks uniqueness against other workspaces, and asks the legacy
  certificate service to verify it.
- The webapp resolves the workspace for an incoming request by the request
  host (`GET /workspaces?custom_domain=<host>`), so whoever can send a
  `Host` header for a domain is served that workspace.
- CORS allowed origins are seeded from custom domains.

## Target design

| Concern | Today | With the custom-domain service |
| --- | --- | --- |
| Registration | write `custom_domain` on the workspace | `POST /v1/domains` with `reference = workspace id`; store the returned domain `id` on the workspace |
| Instructions | none | show the two returned DNS records verbatim, with their help text |
| Verification | boolean set after one check | read `status` and `checks`; `custom_domain_verified` becomes `status == ready` |
| Retry | none | `POST /v1/domains/{id}/checks` behind a "check again" button (rate limited) |
| Serving | resolve workspace by host | resolve workspace from the signed assertion's `ref`; refuse requests without one on the custom-domain path |
| Removal | clear the field | `DELETE /v1/domains/{id}` and clear the field; the hostname is free again immediately |
| Status updates | none | subscribe to `domain.ready`, `domain.attention_required`, `domain.recovered`, `domain.deleted`, or poll |

## Changes in BetterCollected

### Backend

1. Configuration: `CUSTOM_DOMAIN_API_URL`, `CUSTOM_DOMAIN_API_CREDENTIAL`
   (issued with `custom-domain credential issue`), `CUSTOM_DOMAIN_APPLICATION_ID`,
   `CUSTOM_DOMAIN_ASSERTION_KEYS` (`id:secret,...` from the operator) and
   the webhook secret after subscribing. Use the SDK
   (`custom-domain-sdk`).
2. Workspace model: add `custom_domain_id` (the domain id), keep
   `custom_domain` as the display hostname, derive `custom_domain_verified`
   from status. Add `custom_domain_status` and `custom_domain_checks` (JSON)
   for the settings page.
3. `workspace_service`: on set, call `client.create_domain(hostname,
   reference=str(workspace.id), idempotency_key=f"ws-{workspace.id}-{hostname}")`;
   map `hostname_already_claimed` and the hostname validation codes to the
   existing "domain already exists" and validation errors; store id and
   records. On change, delete the old domain first, then create. On delete,
   `client.delete_domain(id)`. Add a `recheck` action calling
   `request_recheck` and returning `RateLimitedError.retry_after` to the UI.
4. Middleware: wrap the app with `CustomDomainMiddleware(keys=...,
   application_id=..., on_missing="passthrough")` because the same backend
   serves `bettercollected.com` itself. On the custom-domain code path (any
   request whose host is not a BetterCollected domain) require
   `request.state.custom_domain` and select the workspace by
   `assertion.reference`; never by host. The middleware also answers the
   workspace probe, so nothing else is needed for readiness.
5. Webhooks: `client.create_webhook(url, [...])`; in the handler verify with
   `verify_webhook`, deduplicate by event id, and update
   `custom_domain_status`/`checks` from `event.domain`. Or poll
   `get_domain` when the settings page opens; both are fine, webhooks avoid
   stale UI.
6. CORS: allow the customer hostname once the domain is `ready` (the
   webhook is the natural trigger).

### Webapp

Replace the host lookup with the assertion: the Next.js server reads
`X-Custom-Domain-Assertion` from the incoming request (the edge sets it and
strips any client copy), verifies it with the same algorithm (the SDK
documents it; a small TypeScript port is straightforward: HMAC-SHA256 over
the first three dot-separated parts, base64url, 60 s TTL with 30 s skew,
compare `app` with the application id), and passes `ref` to
`GET /workspaces/{id}`. Until the port exists, the webapp can call a backend
endpoint that verifies the header and returns the workspace.

Settings page: show `dns_records` (name, type, value, help), the four checks
with their messages, a "check again" button, and the status.

## Migration of existing custom domains

BetterCollected already has workspaces with `custom_domain` set and serving
through the legacy path. Cut over without reassigning anything:

1. Export `{hostname: workspace id}` for every workspace with a non-empty,
   enabled custom domain.
2. Run `custom-domain legacy import --application bettercollected
   --reference-map bc-domains.json --grandfather --dry-run`; resolve every
   skipped name (apex domains and public suffixes cannot be imported and need
   a subdomain or a communicated end date) and then run it for real. The
   import refuses to commit while anything is skipped unless explicitly
   allowed, and it never re-claims a hostname another application holds.
3. Grandfathered domains start in `provisioning` with ownership verified by
   import; the worker's routing, certificate and origin checks then make them
   `ready`, which requires the CNAME to point at the new edge and the
   BetterCollected origin to answer the workspace probe. Deploy the
   middleware before flipping DNS.
4. Store the returned domain ids on the workspaces (`GET /v1/domains` lists
   them with `reference`), then switch the settings page to the new API.
5. Only then set `ENABLE_LEGACY_API=false`. Rollback at any earlier step is
   re-enabling the legacy API and pointing DNS back; nothing in the new
   database is lost.

A hostname is never moved between workspaces by the import: the reference
map is authoritative, and a mismatch shows up as `workspace_mismatch` on the
origin check before the domain becomes ready.

## What is proven in this repository

`tests/test_e2e_production_like.py` runs the real Caddy with on-demand
certificates from its internal CA, the real ask and assert endpoints, the
checks worker with the real HTTPS readiness and workspace probes, and two
origins built with the SDK middleware: a BetterCollected-like application
with two workspaces and the sample application. It asserts that each
hostname serves its own workspace over HTTPS, that an unknown host gets no
certificate, that re-issued ownership instructions (a stale claim) stop
service, and that a deleted domain is refused on the next request while the
other application keeps serving.

## Developer friction found

- The SDK must serve the API over a real socket in tests; the async-only
  ASGI transport of httpx does not fit a synchronous client. Documented in
  the SDK tests.
- Caddy leaves the SNI placeholder unreplaced on plain-HTTP requests; the
  assert endpoint tolerates it, but local setups should prefer
  `EDGE_TLS_ISSUER=internal` with a trusted root over `DISABLE_HTTPS=true`
  so behaviour matches production.
- The workspace probe path must not be intercepted by proxies or auth in
  front of the origin; the SDK middleware answers it before the app sees it.
- Grandfathered claims are verified without a TXT record; the DNS worker
  keeps checking ownership afterwards, so customers must add the TXT record
  within the 24 hour grace period or the domain is suspended. The
  BetterCollected settings page should show that record prominently after
  migration.
