# Operations: edge reconciliation, backup and restore

Companion to [ADR 0001](decisions/0001-certificate-storage.md) and
[data-model.md](data-model.md).

## What holds state

| State | Where | Sensitive content |
| --- | --- | --- |
| Applications, credentials (hashed), origins, domains, ownership claims, checks, events, idempotency keys | Database (`DATABASE_URL`) | Claim tokens (customer-visible but should not leak before the customer sees them), credential hashes, origin verification tokens |
| Certificates, private keys, ACME account keys, issuance locks | Caddy storage: `https_data` volume (`CADDY_STORAGE=file`) or Redis (`CADDY_STORAGE=redis`) | Private keys |
| Caddy bootstrap configuration (`/etc/caddy/bootstrap.json`) | Rendered from the environment on start; readable by the `caddy` user only | Redis password and encryption key when Redis storage is used |
| Running Caddy `apps` configuration | Derived; rebuilt from the database by the reconciler | Hostnames and origins only |
| Legacy `domains/caddy.json` | `https_domains` volume; written only by the legacy API | Hostnames and upstreams |

## Application onboarding

Applications, their origins and their credentials are managed by the operator
with the CLI; there is no self-service endpoint. One verified origin per
application receives all of its customer traffic; v1 has no per-domain
upstream.

1. **Create the application** with the CNAME target its customers will use:
   `custom-domain application create --slug acme --name "Acme Forms" --cname-target acme.edge.example.net`
2. **Register the origin**:
   `custom-domain origin register --application acme --host app.acme.example`
   (scheme `https`, port 443 by default). The command prints a verification
   token. IP literals, wildcards and malformed hosts are rejected.
3. **Prove control of the origin.** The SaaS operator serves the token as the
   plain-text body of
   `GET https://app.acme.example/.well-known/custom-domain-origin-verification`,
   then runs `custom-domain origin verify --application acme --host app.acme.example --activate`.
   The probe resolves the host once, refuses any address that is not publicly
   routable (loopback, private, link-local, cloud metadata, multicast,
   reserved), connects to the resolved address rather than by name so a DNS
   answer cannot change between check and connection, verifies TLS against
   the system trust store with the origin host as SNI, follows no redirects,
   caps the body at 4 KiB and times out after 5 seconds. Failures are recorded
   on the origin (`origin list` shows them) with a stable code:

   | Code | Meaning and fix |
   | --- | --- |
   | `dns_resolution_failed` | The host has no address; check the name. |
   | `private_address_blocked` | Resolves to a non-public address. Only a trusted self-hosted deployment may set `ORIGIN_ALLOW_PRIVATE=true` (or pass `--allow-private`). |
   | `connection_failed` | TCP connection refused or reset on every address. |
   | `timeout` | No connection or no answer within the timeout. |
   | `tls_handshake_failed` | Not speaking TLS on that port, or protocol mismatch. |
   | `tls_verification_failed` | Certificate not trusted or not valid for the host. Fix the certificate; TLS verification is never disabled. |
   | `unexpected_status` | Not HTTP 200 (redirects count as failures). |
   | `token_mismatch` | Body is not the token. |
   | `response_too_large` | Body over 4 KiB. |

   A later failed re-verification deactivates the origin, and the edge stops
   routing the application until it is verified and activated again. The
   same address policy applies on the serving path: at every reconciliation
   the origin's name is resolved again, the edge dials the resolved public
   address (presenting the origin's name as SNI and verifying its
   certificate), and an origin whose DNS now points at a private, link-local
   or metadata address is dropped from the routes instead of being dialed.
4. **Issue a credential**: `custom-domain credential issue --application acme --label backend`.
   The secret is shown once. Clients send it as `Authorization: Bearer`; it
   is never accepted in a query string or cookie.
5. **Rotate** without downtime:
   `custom-domain credential rotate --application acme --id <old id> --grace-hours 24`
   issues a replacement and lets the old credential expire after the grace
   period. `credential revoke` stops one immediately.

## Reconciliation

Caddy starts from a bootstrap configuration (admin listener on
`localhost:2019`, certificate storage, an empty server) that the entrypoint
renders with `custom-domain edge bootstrap`. The reconciler
(`app/edge/reconcile.py`) builds the desired `apps` subtree (routes and TLS
automation) from the database and replaces only that subtree through
`POST /config/apps` when it differs, so the storage settings Caddy was
started with are never touched by the application. It never writes to the
database.

- Runs at application start, every `EDGE_RECONCILE_INTERVAL` seconds
  (default 30), and right after a domain is deleted through the API.
- Enabled when `ENABLE_LEGACY_API=false` (or `EDGE_RECONCILE_ENABLED=true`).
  It refuses to run alongside the legacy `/domains` API, which writes its
  own Caddy configuration; the application fails to start if both are on.
- A hostname is routed when its application is active and has an active
  origin, the domain is live and `ready`, its claim is verified and all four
  checks pass. Deleted, pending, suspended and drifting domains are not
  routed.
- Runs are serialized across instances: each run updates the single row of
  `edge_locks` as the first statement of its transaction and holds that
  transaction until the Caddy write has finished (a row lock on PostgreSQL,
  the write lock on SQLite). A run that starts after a change committed
  therefore always sees it, and a snapshot built before a deletion can never
  be applied after a newer one. On SQLite this briefly blocks all writers
  during each run, one more reason PostgreSQL is the production target.
- Failure modes: `database_unavailable` (nothing sent to Caddy),
  `caddy_unavailable` (admin API unreachable), `config_rejected` (Caddy
  validated and refused the configuration; it keeps the previous one). All
  are logged and retried on the next tick; none change domain state.

Commands:

```
uv run custom-domain edge config              # complete desired configuration, secrets masked
uv run custom-domain edge bootstrap --output /etc/caddy/bootstrap.json  # start-up config, mode 0600
uv run custom-domain edge reconcile --dry-run # route and hostname counts, Caddy not contacted
uv run custom-domain edge reconcile           # apply the apps subtree once; exit 3 on failure
```

## Backup

Back up both stores. Back up the database first; the certificate store can
always be rebuilt by re-issuance, subject to CA rate limits.

**Database (PostgreSQL):**

```
pg_dump --format=custom --file=custom-domain-$(date +%F).dump \
  --dbname="$(custom-domain db libpq-url)"
```

`DATABASE_URL` is a SQLAlchemy URL (`postgresql+psycopg://...`), which
`pg_dump` does not accept; `custom-domain db libpq-url` prints the same
connection as a libpq URL (`postgresql://...`).

Nightly at minimum, plus before every migration (`custom-domain db upgrade`).
The dump contains claim tokens and credential hashes: encrypt it at rest and
restrict access like a secrets file.

**Database (SQLite, single-instance only):**

```
sqlite3 data/custom_domain.db ".backup 'custom-domain-$(date +%F).db'"
```

**Certificate store, file storage:** snapshot the `https_data` volume while
Caddy is running (files are written atomically):

```
docker run --rm -v https_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/caddy-storage-$(date +%F).tgz -C /data .
```

**Certificate store, Redis:** run Redis with `appendonly yes` and take RDB
snapshots (`BGSAVE`) or use the provider's snapshot feature. Alternatively
export from any edge instance with Caddy's own tooling, which works for both
backends. The command needs a configuration that selects the storage to read
from; the bootstrap file the container was started with is exactly that:

```
docker compose exec --user caddy https \
  caddy storage export --config /etc/caddy/bootstrap.json --output /tmp/caddy-storage.tar
docker compose cp https:/tmp/caddy-storage.tar ./caddy-storage-$(date +%F).tar
```

To restore, or to move between file and Redis storage, import with a
bootstrap file that selects the destination storage:

```
docker compose exec --user caddy https \
  caddy storage import --config /etc/caddy/bootstrap.json --input /tmp/caddy-storage.tar
```

This round trip (export from Redis, import into file storage) is exercised
by the container smoke test in the #2 pull request.

Backups of the certificate store contain private keys: encrypt them and keep
them out of general-purpose backup buckets.

## Restore

1. Restore the database (`pg_restore --clean --if-exists` into an empty
   database, or copy the SQLite file into `data/`).
2. Restore the certificate store: untar into the `https_data` volume, restore
   the Redis snapshot, or `caddy storage import`. Skip this step if the
   backup is missing; certificates are re-issued on demand, but plan for CA
   rate limits when many hostnames are involved.
3. Start the stack. The entrypoint runs migrations, starts Caddy, and the
   reconciler applies the configuration derived from the restored database.
4. Verify: `custom-domain edge reconcile --dry-run` reports the expected
   hostname count, `GET /v1/domains` lists the domains, and a ready hostname
   answers over HTTPS with a valid certificate.

Replacing an instance is the same procedure without step 1 when the
database is external, and without step 2 when Redis storage is used.

## Multiple edge instances

Run every edge with the same `DATABASE_URL` and the same Redis storage
settings. Each instance reconciles independently from the same database, so
they converge on the same configuration; Redis locks ensure only one of them
issues a given certificate. Minimal Compose fragment:

```yaml
services:
  redis:
    image: redis:7
    command: ["redis-server", "--appendonly", "yes", "--requirepass", "${CADDY_REDIS_PASSWORD}"]
    volumes: ["redis_data:/data"]
  https:
    image: sireto/custom-domain:latest
    deploy: { replicas: 2 }
    environment:
      ENABLE_LEGACY_API: "false"
      CADDY_STORAGE: redis
      CADDY_REDIS_ADDRESS: redis:6379
      CADDY_REDIS_PASSWORD: ${CADDY_REDIS_PASSWORD}
      CADDY_REDIS_ENCRYPTION_KEY: ${CADDY_REDIS_ENCRYPTION_KEY}
volumes:
  redis_data:
```

Use TLS between edges and Redis (`CADDY_REDIS_TLS=true`) when they do not
share a private network.

## Private key boundaries

The container runs two processes under two system users (see
`entrypoint.sh`):

| | `caddy` user | `app` user (migrations, API, reconciler) |
| --- | --- | --- |
| Certificate store `/var/lib/custom-domain/caddy` (file storage) | owner, mode 0700 | no direct file access |
| `/etc/caddy/bootstrap.json` (storage credentials) | owner, mode 0600 | no direct file access |
| `CADDY_REDIS_PASSWORD`, `CADDY_REDIS_ENCRYPTION_KEY`, `CADDY_REDIS_USERNAME`, `CADDY_REDIS_TLS_SERVER_CERTS_PEM` | in environment | removed from environment |
| Caddy admin API `localhost:2019` | serves it | reachable; used for `GET /config/` and `POST /config/apps` |
| Database | no access | full access |

What the separation gives: the API process cannot read key files or the
storage credentials directly, never handles them in its own code or
environment, and Caddy binds ports 80 and 443 through a file capability
rather than root. What it does not give, stated plainly:

**The Caddy admin API is the trust boundary, and today the API process is
inside it, for both storage kinds.** Caddy's admin API has no
authentication and no per-path or per-payload control. A compromised API
process can `GET /config/` and read the storage block (Redis credentials and
encryption key), and it can `POST /config/apps` or `/load` a configuration
of its own, for example a `file_server` route publishing
`/var/lib/custom-domain/caddy`, then fetch private keys through Caddy
itself. Restricting a proxy to `GET`/`POST /config/apps` does not close
this, because an arbitrary `apps` payload can still install such a route.
Single-instance file storage is therefore **not** protected by the user
separation alone.

Hardening requirement before production use, with either storage kind
(tracked in #12):

1. Run Caddy in its own container. Its admin API must be reachable only by
   a **validating configuration gateway**, not by the API container
   directly, and `/load` must not be reachable at all.
2. The gateway accepts only the shape the reconciler produces: an `apps`
   object whose HTTP server routes contain a single `reverse_proxy` handler
   per route, whose upstream is an origin registered and verified in the
   database, with no other handler modules (`file_server`, `static_response`
   with bodies, `templates`, admin or metrics endpoints), no changes to
   `admin`, `storage` or the TLS automation policy beyond the ACME email,
   and no listener other than the edge ports. Anything else is rejected and
   logged.
3. The certificate store (file volume or Redis) is mounted or reachable
   from the Caddy container only.

Until that gateway exists, treat the management API container as having the
same privileges as Caddy, protect its credentials accordingly, and do not
run it on a host or network where reading customer certificate keys would be
unacceptable.

- The Caddy admin API must never be published outside the container, and the
  management API on port 9000 must not be a public port: the compose file
  binds it to `127.0.0.1` so only the host (or an authenticated reverse proxy
  on the host) reaches it. It is authenticated, but it is also where
  credentials are used and domains are managed, so keep it off the internet.
- Redis: require AUTH, enable TLS when crossing networks, restrict network
  access to the edge instances, and set `CADDY_REDIS_ENCRYPTION_KEY` so
  values are AES-encrypted at rest (values are still decrypted in memory by
  each edge).
- File storage: the `https_data` volume (mounted at `/var/lib/custom-domain`)
  is owned by the `caddy` user with mode 0700; do not bind-mount it into other
  services.
- Rotating the Redis encryption key: `caddy storage export` with the old
  key, change the key, `caddy storage import`.
- Backups that include the certificate store or the database are secrets.
