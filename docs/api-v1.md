# Domain API v1

Status: implemented for issue #1. The machine-readable contract is
[openapi.json](openapi.json) (regenerate with
`uv run custom-domain openapi export --output docs/openapi.json`); the live
document is served at `/v1/openapi.json` with Swagger UI at `/v1/docs`.

The Python SDK ([sdk/README.md](../sdk/README.md)) wraps everything below.

**Base URL.** On a deployment with `PUBLIC_API=true` (the installer's default)
the API is served through the edge at `https://<edge name>/v1`, for example
`https://edge.example.net/v1/domains`; otherwise it is on port 9000 of the
server behind whatever proxy the operator provides. Every call needs the
application's credential as `Authorization: Bearer cd_...`.

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

Credentials are rotated with `custom-domain credential rotate`, which issues a
replacement and keeps the old one valid for a grace period (24 hours by
default) so clients can switch without downtime; `credential revoke` stops
one immediately. Traffic for the application's domains goes to its single
verified origin, enrolled and proven with `custom-domain origin register` and
`origin verify` (see [operations.md](operations.md#application-onboarding)).

### Read-only credentials

A credential has a scope, set when it is issued and never changed:

- **`full`** (the default) allows everything below.
- **`read`** allows `GET` and `HEAD` only: listing and reading domains, their
  checks and DNS records, webhooks and their deliveries. Give one to a
  dashboard, a monitoring job or a support tool, so a leaked key can disclose
  but not delete. Every other method, on every v1 path including
  `/v1/webhooks`, is refused with `403 credential_read_only` before the
  request is acted on.

```
uv run custom-domain credential issue --application acme --label dashboard --scope read
```

Rotating a credential keeps its scope. The per-credential rate limit applies
to read-only credentials as to any other.

## Resources

### Domain

| Field | Meaning |
| --- | --- |
| `id` | Stable UUID. |
| `hostname` | Canonical form: lowercase, punycode, no trailing dot. |
| `reference` | Opaque workspace identifier supplied by the application; returned verbatim and delivered to the origin inside the signed assertion on every request ([edge-routing.md](edge-routing.md)). |
| `status` | `pending_dns`, `provisioning`, `ready`, `attention_required`, `suspended`, `deleting`. |
| `dns_records` | Records the customer must publish: one `TXT` (`purpose: ownership`) and one `CNAME` (`purpose: routing`), each with `name`, `type`, `value` and `help`. Empty once the domain is deleted. |
| `setup` | How the customer will publish the records, from the DNS provider hosting the hostname's zone; see [Setup](#setup). |
| `checks` | One entry per check: `ownership`, `routing`, `certificate`, `origin`, each `pending`, `passing` or `failing` with `error_code`, `message`, `observed_at` and `next_check_at`. |
| `metadata` | Up to 32 scalar values supplied at creation. |
| `created_at`, `updated_at`, `deleted_at` | UTC timestamps. |

A domain is `ready` only when all four checks pass and the ownership claim is
verified; readiness is never inferred from DNS alone. The status model and its
transitions are specified in [data-model.md](data-model.md).

### Setup

Shortly after registration the DNS worker looks up the zone holding the
hostname, identifies its DNS provider from the zone's nameservers, and checks
whether the provider supports Domain Connect. `setup` reports the result, so
the application can show the customer the right instructions:

```json
"setup": {
  "method": "guided",
  "provider": {"id": "cloudflare", "name": "Cloudflare"},
  "zone": "customer.example",
  "host": "forms",
  "guide_url": "https://github.com/sireto/custom-domain/blob/main/docs/dns-guides/cloudflare.md",
  "detected_at": "2026-09-25T14:05:00Z"
}
```

| Field | Meaning |
| --- | --- |
| `method` | `domain_connect`: the provider supports the deployment's Domain Connect template and the deployment can sign Domain Connect requests, so the customer approves the records at their provider in one step. `guided`: the customer adds the records by hand with the provider's own guide at `guide_url`. `manual`: the provider was not recognized or has no specific guide; `guide_url` is the generic one. `null` until the lookup has run. |
| `provider` | `id` and `name` of the DNS provider. Ids are stable: `cloudflare`, `godaddy`, `namecheap`, `route53`, `google-cloud-dns`, `squarespace`, `porkbun`, `hostinger`, `ionos`, `digitalocean`, `vercel`, `netlify`, `namesilo`, `wordpress-com`, `azure-dns`, `gandi`, `ovh`, `hetzner`, `dnsimple`, `name-com`, `linode`, `wix`, and `unknown`. New ids may be added. `null` until the lookup has run. |
| `zone` | The DNS zone the records go in. Until the lookup it is the registrable domain from the Public Suffix List (`acme.co.uk` for `forms.acme.co.uk`); the lookup replaces it with a delegated subzone when there is one. |
| `host` | The hostname relative to `zone` (`forms`, or `forms.eu` for `forms.eu.acme.co.uk`): what most DNS providers want in the record's Name or Host field. |
| `guide_url` | Setup instructions for the provider (`DNS_GUIDE_URL`). `null` until the lookup has run. |
| `detected_at` | When the zone was last looked up. Lookups are cached per zone for 24 hours (an hour after a temporary failure). |

`setup` never affects the checks or the status: it only says how the records
are likely to be published. The lookup runs in the DNS worker, never while
answering a request, and a failed lookup leaves the previous answer (or
`null`) in place. `method` is `domain_connect` only on deployments that can
sign Domain Connect requests; on others, a provider that supports the
template is reported as `guided`.

### One-step setup (Domain Connect)

When `setup.method` is `domain_connect`, `POST /v1/domains/{id}/connect`
returns a signed apply URL at the customer's DNS provider. Send the customer
there; they approve the two records in one step.

```bash
curl -X POST https://edge.example.net/v1/domains/$DOMAIN_ID/connect \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"redirect_uri": "https://app.example/settings/domains?done=1", "state": "s-42"}'
```

```json
{
  "url": "https://dash.cloudflare.com/domainconnect/v2/domainTemplates/providers/example.com/services/hostname/apply?domain=customer.example&host=forms&target=edge.example.net&token=...&redirect_uri=...&state=...&key=_dcpubkeyv1&sig=...",
  "provider": {"id": "cloudflare", "name": "Cloudflare"},
  "expires_at": "2026-09-25T15:00:00Z"
}
```

| Field | Meaning |
| --- | --- |
| `redirect_uri` (request, optional) | Where the provider sends the customer when they are done: `https`, with a host, no user name and no fragment, at most 512 bytes. It is part of a signed URL, so it is your responsibility: point it at a page of your own. |
| `state` (request, optional) | Returned to `redirect_uri` unchanged, at most 512 bytes without control characters. The customer arrives with `error=access_denied` as well if they declined. |
| `url` | The signed apply URL; `sig` is its last parameter. |
| `provider` | The DNS provider it goes to. |
| `expires_at` | Ask for a new URL after this time (an hour). |

The request is a `POST`, so a read-only key is refused, and it is limited to
10 links per domain and 120 per application per hour (`429 rate_limited`).
It returns `409 domain_connect_unavailable` when `setup.method` is not
`domain_connect`, when the domain is suspended or deleting, or when the
signer refuses the domain (`details.reason`), and
`503 domain_connect_signer_unavailable` when the signer cannot be reached
(retry shortly).

**Never trust the return.** Whatever the provider's redirect says, the
service checks DNS itself: for 30 minutes after a link is issued the DNS
checks run every 30 seconds, and when the deployment has a public address
the customer passes through the service on the way back, which checks at
once. The outcome arrives as usual, by polling the domain or by the
`domain.ready` webhook. An application that handles the return itself and
wants a check at a moment of its choosing calls
`POST /v1/domains/{id}/checks`. How the deployment signs, the return address
and the key are in [domain-connect.md](domain-connect.md).

## Endpoints

| Method and path | Purpose | Success |
| --- | --- | --- |
| `POST /v1/domains` | Register a hostname for a workspace. | `201` domain, or `200` replay |
| `GET /v1/domains` | List domains; filter by `reference`, `status`, `include_deleted`; paginate with `limit` and `offset`. | `200` page |
| `GET /v1/domains/{id}` | One domain with current checks. | `200` domain |
| `POST /v1/domains/{id}/checks` | Ask for all checks to run again. | `202` domain |
| `POST /v1/domains/{id}/connect` | A signed Domain Connect apply URL ([one-step setup](#one-step-setup-domain-connect)). | `200` `{url, provider, expires_at}` |
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
  "setup": {"method": null, "provider": null, "zone": "customer.example", "host": "forms",
            "guide_url": null, "detected_at": null},
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

The `error_code` values of the ownership and routing checks and the rules
that move a domain between statuses are specified in
[dns-verification.md](dns-verification.md).

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
| 409 | `domain_limit_reached` | The application or the whole deployment already has as many live domains as its limit allows. `details` gives `scope` (`application` or `deployment`), `limit` and `live`. Domains already registered keep working; delete one or ask the operator to raise the limit. |
| 409 | `domain_connect_unavailable` | One-step setup cannot be offered for this domain: `setup.method` is not `domain_connect` (`details.method`), the domain is suspended or deleting (`details.status`), or the signer refused it (`details.reason`). Follow `setup.guide_url` instead. |
| 503 | `domain_connect_signer_unavailable` | The Domain Connect signer could not be reached or answered badly (`details.reason`). Retry shortly. |
| 409 | `invalid_status_transition` | The operation does not apply to the domain's current status (for example rechecking a deleted domain). |
| 409 | `idempotency_request_in_progress` | The original request with this key has not finished. |
| 422 | `validation_error` | Body or query does not match the schema. `details.errors` lists locations. Unknown fields such as `upstream` are rejected. |
| 422 | `empty_hostname`, `wildcard_not_supported`, `ip_literal_not_supported`, `hostname_too_long`, `invalid_label`, `apex_not_supported`, `invalid_hostname` | The hostname cannot be registered. `details.field` is `hostname`. |
| 422 | `invalid_reference` | The reference is empty or longer than 255 characters. |
| 422 | `idempotency_key_reused` | The key was used with a different body within the last 24 hours. |
| 429 | `rate_limited` | Too many manual rechecks or Domain Connect links for this domain or application, too many requests with an invalid credential from the client's address (`V1_AUTH_FAILURES_PER_MINUTE`, default 30 a minute), or more requests with this credential than the deployment allows (`V1_REQUESTS_PER_MINUTE`, off by default; counted per credential in any 60 seconds). `Retry-After` says how long to wait. |
| 403 | `application_suspended` | The application was suspended while the request ran. |
| 403 | `credential_read_only` | The credential has the `read` scope and the request is not a `GET` or `HEAD`. Nothing was changed. `details` gives `scope` and `method`. Use a `full` credential for changes. |

## Webhooks

Applications subscribe to `domain.ready`, `domain.attention_required`,
`domain.recovered` and `domain.deleted` with `POST /v1/webhooks`; delivery,
retries, rotation and replay are specified in [webhooks.md](webhooks.md).
Every delivery is a `WebhookEvent`:

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
