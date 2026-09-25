# Operations: edge reconciliation, backup and restore

Companion to [ADR 0001](decisions/0001-certificate-storage.md) and
[data-model.md](data-model.md).

## What holds state

| State | Where | Sensitive content |
| --- | --- | --- |
| Applications, credentials (hashed), origins, domains, ownership claims, checks, events, idempotency keys | Database (`DATABASE_URL`) | Claim tokens (customer-visible but should not leak before the customer sees them), credential hashes, origin verification tokens |
| Certificates, private keys, ACME account keys, issuance locks | Caddy storage: `https_data` volume (`CADDY_STORAGE=file`) or Redis (`CADDY_STORAGE=redis`) | Private keys |
| Running Caddy configuration | Derived; rebuilt from the database by the reconciler | Redis password when Redis storage is used |
| Legacy `domains/caddy.json` | `https_domains` volume; written only by the legacy API | Hostnames and upstreams |

## Reconciliation

The reconciler (`app/edge/reconcile.py`) builds the desired Caddy
configuration from the database and replaces Caddy's configuration through
the admin API only when it differs. It never writes to the database.

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
uv run custom-domain edge config              # desired configuration, secrets masked
uv run custom-domain edge reconcile --dry-run # route and hostname counts, Caddy not contacted
uv run custom-domain edge reconcile           # apply once; exit 3 on failure
```

## Backup

Back up both stores. Back up the database first; the certificate store can
always be rebuilt by re-issuance, subject to CA rate limits.

**Database (PostgreSQL):**

```
pg_dump --format=custom --file=custom-domain-$(date +%F).dump "$DATABASE_URL"
```

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
export from any edge instance, which works for both backends:

```
docker compose exec https caddy storage export --config /dev/null --output /tmp/storage.tar
```

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

- Only the Caddy process reads private keys. The application, the reconciler
  and the CLI never handle key material; the database has no column for it.
- The Caddy admin API listens on `localhost:2019` inside the container and
  must not be published. The derived configuration carries the Redis
  password; `edge config` masks it and logs never print the configuration.
- Redis: require AUTH, enable TLS when crossing networks, restrict network
  access to the edge instances, and set `CADDY_REDIS_ENCRYPTION_KEY` so
  values are AES-encrypted at rest (values are still decrypted in memory by
  each edge).
- File storage: the `https_data` volume is readable by root in the container
  only; do not bind-mount it into other services.
- Rotating the Redis encryption key: `caddy storage export` with the old
  key, change the key, `caddy storage import`.
- Backups that include the certificate store or the database are secrets.
