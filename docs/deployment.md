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
management API is not published (reach it from the host or through a
reverse proxy you authenticate). `EDGE_ASK_TRUSTED_HOSTS` is the `control`
subnet so only edge containers can call the internal endpoints, and
`EDGE_TOKEN` is additionally required on the assert, origins and metrics
calls.

### The configuration gateway

Caddy's admin API in the edge container listens on `127.0.0.1:2020` and is
reachable by nothing outside that container. The gateway
(`custom-domain edge gateway`) listens on `:2019` and is what the reconciler
talks to (`CADDY_ADMIN_URL=http://edge:2019`). It exposes only
`GET /config/`, `GET /config/apps` (the `apps` subtree, never `admin` or
`storage`) and `POST /config/apps`, and it accepts a `POST` only when the
payload is exactly the reconciler's shape: the health and 404 routes, and
per application a route whose handlers are the header strip, the assert
subrequest to the configured upstream, and a `reverse_proxy` to an upstream
that the management API currently lists as a verified active origin
(`GET /internal/edge/origins`), with only the documented headers and
verified TLS. `file_server`, extra listeners, extra servers, storage or TLS
policy changes and `/load` are refused. This closes the boundary described
in [operations.md](operations.md#private-key-boundaries): a compromised API
or worker can no longer make Caddy serve the certificate store.

## Secrets

Copy `deploy/env.production.example` to `deploy/.env.production` and fill it.
It holds the database password, `EDGE_ASSERTION_KEYS` (signs assertions),
`EDGE_TOKEN` (edge to API), `CADDY_REDIS_PASSWORD` and
`CADDY_REDIS_ENCRYPTION_KEY`. Only the edge containers need the Redis
values; the entrypoint scrubs them from the Python processes' environment.
Application credentials and webhook secrets are issued by the CLI and shown
once. Logs never carry any of these: the code does not log them, and a
redaction filter masks credential-looking values as a last line of defence.

## Bring-up

```
cd deploy
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
