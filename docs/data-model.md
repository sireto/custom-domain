# Data model and tenant boundary

Status: implemented for issue #4. Later issues build on it: #1 (API), #2
(persistence and certificate storage), #5 (auth and origins), #6 (DNS), #7
(TLS), #8 (routing), #9 (lifecycle).

## Decisions

- **The database is the source of truth.** Applications, credentials, origins,
  domains, ownership claims, check results and events live in a relational
  database managed with SQLAlchemy 2 and Alembic. Caddy configuration becomes
  derived state (implemented in #7 to #9); the current practice of persisting
  the whole Caddy JSON as truth stops at cutover.
- **PostgreSQL in production, SQLite for development and tests.** The schema
  uses only portable features (partial unique indexes, CHECK constraints,
  JSON, UUID). Every test runs on both backends in CI. Issue #2 records the
  final decision together with certificate storage; nothing here prevents
  changing it.
- **Rows are never reused.** A deleted domain leaves a tombstone. Claiming the
  hostname again creates a new row with a new ownership token, so stale
  verification can never be carried over to a different owner or workspace.

## Entities

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `applications` | One SaaS product. The tenant boundary. Holds the application-specific CNAME target customers point at. | unique `slug` |
| `verified_origins` | Where the application's traffic is proxied. Verification and activation are separate steps. | unique `(application_id, scheme, host, port)`; partial unique on `application_id` where `is_active`, so one active origin per application |
| `api_credentials` | Application-scoped secrets. Only the SHA-256 of the random secret is stored; the plaintext is shown once at issue time. `key_prefix` is a non-secret identifier for logs. Revocable and optionally expiring. | unique `key_hash` |
| `domains` | A customer hostname registered by one application for one opaque workspace `reference`. | partial unique on `hostname` where `deleted_at IS NULL`; FK to application is `RESTRICT` |
| `ownership_claims` | The TXT token and CNAME target a customer must publish. `verification_method` is `dns_txt` or `legacy_import`. | unique `token`; partial unique on `domain_id` where `status <> 'revoked'`, so one live claim per domain |
| `domain_checks` | Current state of each of the four checks: `ownership`, `routing`, `certificate`, `origin`. Carries `error_code`, `message`, `details`, `observed_at`, `next_check_at`. | unique `(domain_id, check_type)` |
| `domain_events` | Append-only history tagged with `application_id`. Feeds the status page and, later, webhook delivery (#10). | indexed by application and by domain |

All tables carry `created_at`; mutable tables also carry `updated_at`. Every
timestamp is stored in UTC and returned timezone-aware on both backends.

## Invariants enforced by the database

1. **Exact hostname uniqueness across applications.** Only one live `domains`
   row per hostname. Concurrent creates race in the database; exactly one
   wins and the others receive `hostname_already_claimed`. The insert runs in
   a savepoint so the caller's session stays usable.
2. **One live ownership claim per domain.** Re-issuing instructions revokes
   the previous claim first. Tokens are globally unique.
3. **One active origin per application.** Activation deactivates the previous
   origin in a separate flush so the index never sees two active rows.
4. **Enumerations are CHECK constraints.** Unknown statuses cannot be written
   by any client, including raw SQL.

## Application scoping

Every service function takes the calling `Application`. Reads, listings,
deletes and claim re-issues filter by `application_id`; a domain that belongs
to another application is reported as `domain_not_found`, never as forbidden,
so one tenant cannot probe for another tenant's hostnames. Events copy the
owning `application_id` so webhook fan-out (#10) can filter without joins.
Credentials resolve to exactly one application; the application is never
taken from a request field.

## Hostname canonical form

`app/hostname.py` produces the stored form: trimmed, lowercase, IDNA (punycode)
encoded, no trailing dot. It rejects wildcards, IP literals, malformed labels,
names longer than 253 characters, numeric top-level labels and names with fewer
than three labels. Each rejection has a stable code. Public-suffix based apex
detection and reserved-name policy are part of #12; the three-label rule is
the placeholder until then. Operator-controlled hosts (CNAME targets, origins)
use the same function with `allow_apex=True`.

## Status model

| Status | Meaning |
| --- | --- |
| `pending_dns` | Registered; waiting for the customer to publish TXT and CNAME. |
| `provisioning` | Ownership proven; certificate, routing and origin checks in progress. |
| `ready` | All four checks pass and the live claim is verified. The only serveable state. |
| `attention_required` | A check that previously passed now fails, or a check needs customer action. |
| `suspended` | Serving stopped by policy (ownership lost, application suspended, abuse). |
| `deleting` | Tombstoned. Terminal. |

Allowed transitions (`app/services/domains.py`, `ALLOWED_TRANSITIONS`):

```
pending_dns        -> provisioning | attention_required | suspended | deleting
provisioning       -> ready | pending_dns | attention_required | suspended | deleting
ready              -> attention_required | suspended | deleting
attention_required -> pending_dns | provisioning | ready | suspended | deleting
suspended          -> pending_dns | provisioning | deleting
deleting           -> (none)
```

Entering `ready` additionally requires the live claim to be `verified` and
all four checks to be `passing`. Readiness is never inferred from DNS alone;
the reconciliation worker (#9) records the HTTPS probe as the `routing` and
`certificate` checks before it can call `transition_status(..., READY)`.

## Serveability

The edge (TLS authorization in #7, routing in #8) uses one rule:

```
serveable = deleted_at IS NULL AND status = 'ready' AND live claim is verified
```

`find_live_by_hostname` returns the single live row for a canonical hostname,
and `is_serveable` applies the rule. Unknown, pending, suspended and deleted
names are therefore denied a certificate and never routed.

## Deletion, tombstones and retention

`delete_domain` sets `status = deleting`, `deleted_at = now`, `purge_after =
now + 90 days`, revokes the live claim and records `domain.deleted`. It is
idempotent and terminal: no transition or verification applies to a
tombstone. The hostname is immediately claimable again, by any application,
and that claim gets a new domain id and a new token.

If a customer leaves their old DNS records in place after deletion, the edge
lookup finds no live row for the hostname, so no certificate is issued and no
request is proxied. Removal of the derived Caddy route is the reconciliation
worker's job (#9); until that lands the serveability rule alone is the guard.

Tombstones and their claims, checks and events are kept for 90 days for
audit and support, then hard-deleted by `custom-domain domain purge-tombstones`
(schedule it with the reconciliation worker in #9). Webhook deliveries (#10)
must snapshot event payloads rather than reference rows, because purge
cascades to events.

Reassignment between applications or workspaces is only ever delete followed
by a fresh claim. There is no in-place transfer, so a verified claim never
changes hands.

## Credentials

`issue_credential` returns the plaintext once. Storage keeps the SHA-256
digest and an 11-character prefix. `authenticate_credential` rejects unknown,
revoked and expired secrets and secrets of suspended applications, and
records `last_used_at`. Issue #5 moves the API to a single `Authorization`
header backed by this table and removes the global key.

## Operator commands

```
uv run custom-domain db upgrade
uv run custom-domain application create --slug acme --name "Acme Forms" --cname-target acme.edge.example.net
uv run custom-domain credential issue --application acme --label backend
uv run custom-domain origin register --application acme --host app.acme.example
uv run custom-domain legacy import --application acme --file domains/caddy.json --grandfather --reference-map refs.json
uv run custom-domain domain purge-tombstones
```

`DATABASE_URL` selects the database, for example
`postgresql+psycopg://user:password@db:5432/custom_domain`. The default is a
SQLite file under `data/`.

## Migration from volume-based deployments

Existing deployments keep a full Caddy config in the `https_domains` volume
(`domains/caddy.json`) and certificates in `https_data`. The cutover is
staged so each step can be rolled back by redeploying the previous image.

1. **Deploy this version.** The entrypoint runs `db upgrade` on start. The
   legacy `/domains` endpoint still works and still writes `caddy.json`;
   nothing reads the new tables yet. Mount a `https_db` volume at `/app/data`
   or set `DATABASE_URL` to PostgreSQL. Rollback: previous image, no data
   change.
2. **Register the application.** Create the application whose origin is the
   current `SAAS_UPSTREAM`, register and verify that origin, and issue a
   credential for the SaaS backend. Rollback: drop the rows.
3. **Import hostnames.** Export a reference map from the SaaS
   (`{"forms.customer.example": "<workspace id>"}`), then run `legacy import`.
   With `--grandfather`, imported claims are marked verified by import and
   the domain starts in `provisioning`; without it, customers must publish
   the TXT record before service resumes through the new path. The import
   skips hostnames that are already claimed or invalid, so it is safe to
   re-run. Rollback: delete the imported domains; `caddy.json` is untouched.
4. **Switch the edge to derived configuration** (#7 to #9) and retire the
   legacy endpoint per the plan in #1. Only after this step does the database
   drive Caddy. Keep the `https_domains` volume until the switch has run
   cleanly for one certificate renewal cycle.

Open points handed to later issues:

- Revalidation policy for `legacy_import` claims (#6): CNAME-only until the
  customer adds a TXT record, or a deadline after which service is suspended.
- Public-suffix apex detection and reserved names (#12).
- Certificate storage and multi-instance coordination (#2).
