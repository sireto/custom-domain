# Domain API v1

Status: implemented for issue #1. The machine-readable contract is
[openapi.json](openapi.json) (regenerate with
`uv run custom-domain openapi export --output docs/openapi.json`); the live
document is served at `/v1/openapi.json` with Swagger UI at `/v1/docs`.

## Applications and credentials

An application is a SaaS product that integrates with the service. There is no
self-service registration: the operator creates the application and issues its
credentials.

```
uv run custom-domain application create --slug acme --name "Acme Forms" --cname-target acme.edge.example.net
uv run custom-domain credential issue --application acme --label backend
```

The credential is shown once. Send it on every request:

```
Authorization: Bearer cd_...
```

The application is derived from the credential and never from a request
field. Revoked or expired credentials, and credentials of a suspended
application, are rejected with `401 unauthorized`. Credentials are not
accepted in the query string or in cookies.

## Resources

### Domain

| Field | Meaning |
| --- | --- |
| `id` | Stable UUID. |
| `hostname` | Canonical form: lowercase, punycode, no trailing dot. |
| `reference` | Opaque workspace identifier supplied by the application; returned verbatim and forwarded to the origin as tenant context (#8). |
| `status` | `pending_dns`, `provisioning`, `ready`, `attention_required`, `suspended`, `deleting`. |
| `dns_records` | Records the customer must publish: one `TXT` (`purpose: ownership`) and one `CNAME` (`purpose: routing`), each with `name`, `type`, `value` and `help`. Empty once the domain is deleted. |
| `checks` | One entry per check: `ownership`, `routing`, `certificate`, `origin`, each `pending`, `passing` or `failing` with `error_code`, `message`, `observed_at` and `next_check_at`. |
| `metadata` | Up to 32 scalar values supplied at creation. |
| `created_at`, `updated_at`, `deleted_at` | UTC timestamps. |

A domain is `ready` only when all four checks pass and the ownership claim is
verified; readiness is never inferred from DNS alone. The status model and its
transitions are specified in [data-model.md](data-model.md).

## Endpoints

| Method and path | Purpose | Success |
| --- | --- | --- |
| `POST /v1/domains` | Register a hostname for a workspace. | `201` domain, or `200` replay |
| `GET /v1/domains` | List domains; filter by `reference`, `status`, `include_deleted`; paginate with `limit` and `offset`. | `200` page |
| `GET /v1/domains/{id}` | One domain with current checks. | `200` domain |
| `POST /v1/domains/{id}/checks` | Ask for all checks to run again. | `202` domain |
| `DELETE /v1/domains/{id}` | Stop serving, revoke ownership, tombstone. | `202` domain |

### Register

```
POST /v1/domains
Idempotency-Key: 5c0d3d7c-registration-1
{"hostname": "Forms.Customer.Example", "reference": "ws_8f3a1c", "metadata": {"plan": "pro"}}
```

Response `201`:

```json
{
  "id": "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f",
  "hostname": "forms.customer.example",
  "reference": "ws_8f3a1c",
  "status": "pending_dns",
  "dns_records": [
    {"name": "_custom-domain-challenge.forms.customer.example", "type": "TXT",
     "value": "custom-domain-verify=...", "purpose": "ownership", "help": "..."},
    {"name": "forms.customer.example", "type": "CNAME",
     "value": "acme.edge.example.net", "purpose": "routing", "help": "..."}
  ],
  "checks": [
    {"type": "ownership", "status": "pending", "error_code": null, "message": null,
     "observed_at": null, "next_check_at": null},
    {"type": "routing", "status": "pending", "...": "..."},
    {"type": "certificate", "status": "pending", "...": "..."},
    {"type": "origin", "status": "pending", "...": "..."}
  ],
  "metadata": {"plan": "pro"},
  "created_at": "2026-09-25T14:00:00Z",
  "updated_at": "2026-09-25T14:00:00Z",
  "deleted_at": null
}
```

Show the customer both records exactly as returned. The `help` text covers
the common console mistakes (host-only name fields, quoting, leftover A
records, CDN proxying).

There is no `upstream`. Traffic goes to the application's verified origin
(#5); a per-domain proxy target is not part of v1.

### Waiting for DNS

`GET /v1/domains/{id}` while the customer has not published the records:

```json
{"status": "pending_dns",
 "checks": [
   {"type": "ownership", "status": "failing", "error_code": "txt_record_not_found",
    "message": "No TXT record named _custom-domain-challenge.forms.customer.example was found",
    "observed_at": "2026-09-25T14:05:00Z", "next_check_at": "2026-09-25T14:10:00Z"},
   {"type": "routing", "status": "failing", "error_code": "cname_not_found", "...": "..."},
   {"type": "certificate", "status": "pending", "...": "..."},
   {"type": "origin", "status": "pending", "...": "..."}
 ], "...": "..."}
```

After the customer fixes DNS, call `POST /v1/domains/{id}/checks` to bring the
next check forward instead of waiting for the poll interval. The response is
the domain as it is now; the outcome arrives by polling or webhook.

Manual rechecks are rate limited: at most one per domain every 60 seconds
and 60 per application per hour. Over either limit the response is
`429 rate_limited` with a `Retry-After` header (also given as
`details.retry_after_seconds`). Decisions are serialized per application, so
a burst of concurrent requests cannot exceed the limit. A deleted domain
cannot be rechecked and returns `409 invalid_status_transition`.

### Ready

```json
{"status": "ready",
 "checks": [
   {"type": "ownership", "status": "passing", "observed_at": "2026-09-25T15:00:00Z", "...": "..."},
   {"type": "routing", "status": "passing", "...": "..."},
   {"type": "certificate", "status": "passing", "...": "..."},
   {"type": "origin", "status": "passing", "...": "..."}
 ], "...": "..."}
```

The edge serves the hostname over HTTPS and forwards `reference` to the
origin. A `domain.ready` webhook is emitted.

### DNS drift

If the customer later changes the CNAME, the routing check fails, the domain
moves to `attention_required`, serving stops, and `domain.attention_required`
is emitted:

```json
{"status": "attention_required",
 "checks": [
   {"type": "ownership", "status": "passing", "...": "..."},
   {"type": "routing", "status": "failing", "error_code": "cname_target_mismatch",
    "message": "CNAME points to old-host.example, expected acme.edge.example.net",
    "observed_at": "2026-10-02T09:30:00Z", "next_check_at": "2026-10-02T09:45:00Z"},
   {"type": "certificate", "status": "passing", "...": "..."},
   {"type": "origin", "status": "passing", "...": "..."}
 ], "...": "..."}
```

When the record is restored and the checks pass again the domain returns to
`ready` and `domain.recovered` is emitted.

### Deletion

`DELETE /v1/domains/{id}` returns `202` with the tombstone:

```json
{"status": "deleting", "dns_records": [], "deleted_at": "2026-10-10T08:00:00Z", "...": "..."}
```

The hostname stops serving, its ownership claim is revoked and it disappears
from listings (`include_deleted=true` still shows it). The hostname can be
registered again immediately, by any application, and receives new DNS
records. Deleting twice is a no-op. `domain.deleted` is emitted.

## Idempotency

Send `Idempotency-Key` (up to 255 characters, unique per application) on
`POST /v1/domains`. A retry with the same key and the same body returns the
domain created by the first attempt with status `200` and
`Idempotent-Replayed: true`. The same key with a different body returns
`422 idempotency_key_reused`. A retry that arrives while the first attempt is
still running returns `409 idempotency_request_in_progress`; retry after a
short delay. Keys expire 24 hours after they were first used; after that the
same key starts a new request, whatever its body. Failed creates do not
consume the key.

## Pagination and filtering

`GET /v1/domains` takes `limit` (1 to 200, default 50) and `offset` (default
0), returns `items` in creation order and `next_offset` for the next page or
`null` on the last one. Filters: `reference` (exact match), `status`, and
`include_deleted`.

## Errors

Every error has the same body:

```json
{"error": {"code": "hostname_already_claimed", "message": "forms.customer.example is already claimed", "details": {}}}
```

| Status | `code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing, malformed, unknown, revoked or expired credential, or suspended application. `WWW-Authenticate: Bearer` is set. |
| 404 | `domain_not_found` | No such domain in the calling application, including domains of other applications and deleted domains without `include_deleted`. |
| 409 | `hostname_already_claimed` | The hostname is live in this or another application. |
| 409 | `invalid_status_transition` | The operation does not apply to the domain's current status (for example rechecking a deleted domain). |
| 409 | `idempotency_request_in_progress` | The original request with this key has not finished. |
| 422 | `validation_error` | Body or query does not match the schema. `details.errors` lists locations. Unknown fields such as `upstream` are rejected. |
| 422 | `empty_hostname`, `wildcard_not_supported`, `ip_literal_not_supported`, `hostname_too_long`, `invalid_label`, `apex_not_supported`, `invalid_hostname` | The hostname cannot be registered. `details.field` is `hostname`. |
| 422 | `invalid_reference` | The reference is empty or longer than 255 characters. |
| 422 | `idempotency_key_reused` | The key was used with a different body within the last 24 hours. |
| 429 | `rate_limited` | Too many manual rechecks for this domain or application. `Retry-After` says how long to wait. |
| 403 | `application_suspended` | The application was suspended while the request ran. |

## Webhooks

Applications subscribe to `domain.ready`, `domain.attention_required`,
`domain.recovered` and `domain.deleted` (subscription management and delivery
are implemented in #10). Every delivery is a `WebhookEvent`:

```json
{
  "id": "b7e2c1a0-9d8f-4e7a-b6c5-d4e3f2a1b0c9",
  "type": "domain.ready",
  "created_at": "2026-09-25T15:00:00Z",
  "data": {"domain": {"id": "6f1c2d3e-...", "hostname": "forms.customer.example",
                      "reference": "ws_8f3a1c", "status": "ready", "checks": ["..."], "...": "..."}}
}
```

`data.domain` is the full domain resource at the time of the event, so a
consumer never needs a follow-up read to know the hostname, workspace
reference and check results. Deliveries carry
`X-Custom-Domain-Signature: t=<unix time>,v1=<hex HMAC-SHA256>` over
`<t>.<raw body>` using the application's webhook secret, which is distinct
from its API credential. Consumers must reject deliveries older than five
minutes, deduplicate on `id`, and tolerate out-of-order delivery by comparing
`created_at`. The four events are also listed under `webhooks` in the OpenAPI
document with the payload schema.

## Migration from the legacy `/domains` API

The legacy endpoints (`GET/POST/DELETE /domains?domain=...&upstream=...` with
the global `API_KEY`) keep working in this release so existing deployments can
move at their own pace. They are marked deprecated in the OpenAPI document and
every response carries `Deprecation: true` and
`Link: </v1/docs>; rel="successor-version"`.

| Legacy | v1 |
| --- | --- |
| Global `API_KEY` in query, header or cookie | Application credential in `Authorization: Bearer` |
| `POST /domains?domain=X&upstream=Y` | `POST /v1/domains` with `hostname` and `reference`; no `upstream` (traffic goes to the application's verified origin) |
| `GET /domains` (list of hostnames) | `GET /v1/domains` (domain resources with status and checks) |
| `DELETE /domains?domain=X` | `DELETE /v1/domains/{id}` |
| Serving starts as soon as the route is added | Serving starts when ownership, routing, certificate and origin checks pass |

Steps for an existing deployment:

1. Deploy this release. Nothing changes for legacy clients.
2. Register the application, verify its origin and issue a credential
   (`custom-domain application create`, `origin register`, `credential issue`).
3. Import the existing hostnames from `domains/caddy.json` with
   `custom-domain legacy import --reference-map refs.json` (see
   [data-model.md](data-model.md#migration-from-volume-based-deployments)).
   Every hostname needs its workspace reference; the import refuses to commit
   while any hostname is skipped unless `--allow-skipped` is passed.
4. Switch the client to v1: send the credential, pass `reference`, drop
   `upstream`, and read `status` and `checks` instead of assuming a new
   hostname serves immediately. Add `Idempotency-Key` to creates that may be
   retried.
5. Set `ENABLE_LEGACY_API=false` and redeploy. The legacy routes return 404,
   the global `API_KEY` is no longer needed, and the edge reconciler starts
   deriving the Caddy configuration from the database
   ([operations.md](operations.md#reconciliation)).
6. The legacy endpoints are removed in a later release once #7 to #9 make
   imported domains reach `ready` through the new path; a release note will
   announce the version.

Rollback at any step before 6: set `ENABLE_LEGACY_API=true` (the default) and
redeploy; the legacy path still reads and writes `domains/caddy.json`.
