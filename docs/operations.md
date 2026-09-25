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
| Certificate store `/var/lib/custom-domain/caddy` (file storage) | owner, mode 0700 | no access |
| `/etc/caddy/bootstrap.json` (storage credentials) | owner, mode 0600 | no access |
| `CADDY_REDIS_PASSWORD`, `CADDY_REDIS_ENCRYPTION_KEY`, `CADDY_REDIS_USERNAME`, `CADDY_REDIS_TLS_SERVER_CERTS_PEM` | in environment | removed from environment |
| Caddy admin API `localhost:2019` | serves it | uses it for `GET /config/` and `POST /config/apps` |
| Database | no access | full access |

What this guarantees:

- With file storage (the default), a compromised API process cannot read
  private keys: the store is owned by `caddy` with mode 0700, the API runs
  as `app`, and the database has no column for key material.
- The API never handles storage credentials: it only writes the `apps`
  subtree, and its environment is scrubbed before it starts.
- Caddy binds ports 80 and 443 through a file capability, not root.

Residual risk, stated plainly: Caddy's admin API has no per-path access
control and `GET /config/` returns the full configuration, including the
storage block. With Redis storage, a compromised API process can therefore
read the Redis credentials and encryption key from the admin API and reach
the certificate store over the network. The application does not need that
endpoint's storage section, but the admin API cannot hide it.

Hardening requirement before production use with Redis storage (tracked in
#12): run Caddy in its own container, expose its admin API to the API
container only through a reverse proxy that allows `GET /config/apps` and
`POST /config/apps` (and nothing else), and keep the Redis network reachable
from the Caddy containers only. Single-instance deployments with file storage
already meet the boundary described above.

- The Caddy admin API must never be published outside the container.
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
