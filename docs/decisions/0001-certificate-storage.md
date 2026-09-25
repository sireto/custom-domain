# ADR 0001: Where Caddy keeps certificates, keys and ACME locks

Status: accepted (issue #2), 2026-09-25.

## Context

The service terminates TLS for customer hostnames in Caddy. Caddy (through
CertMagic) needs a storage backend for certificates, private keys, ACME
account keys and the locks that stop two instances from issuing the same
certificate at the same time. Today that is Caddy's default file storage in
the `https_data` volume of a single container.

Issue #2 asked whether the originally proposed Redis storage module should
hold this state, and whether it should hold application and domain state as
well. Issue #4 already made the relational database (PostgreSQL in
production) the authoritative store for applications, credentials, domains,
claims, checks and events. This record covers the certificate side and the
relationship between the two.

## Required properties

Measured against how CertMagic uses its storage interface:

| Property | Why it matters |
| --- | --- |
| Atomic `Store`, `Load`, `Delete`, `Exists`, `List`, `Stat` | CertMagic lists directories to find certificates and expects partial writes never to be visible. |
| Distributed lock with expiry and renewal | Issuance and renewal take a per-hostname lock; without a shared lock two edges both talk to the CA, doubling rate-limit use and racing on the stored certificate. |
| Durable across restarts and replacements | Re-issuing every certificate after a restart hits CA rate limits and causes handshake failures. |
| Encryption at rest and narrow access | The store holds private keys; only the Caddy process should read them. |
| Backup and restore | Losing the store is recoverable (re-issuance) but slow and rate-limited; restore should be routine. |
| Operational simplicity | Small objects (a few KB per hostname), low request rate; latency is irrelevant. |

Application and domain state has different requirements: relational
integrity, unique constraints under concurrency, audit history, and reads
from the API and workers. That is what the database already provides.

## Options

**A. File storage on a local volume (today).** Meets every property for one
edge instance. Fails locking and durability as soon as a second instance
runs, because each instance has its own files. Keys are readable by anyone
with access to the volume.

**B. File storage on a shared network filesystem.** Rejected. CertMagic's
file locks rely on atomic rename and lock-file semantics that NFS and most
network filesystems do not guarantee, which is exactly the failure this
issue must prevent.

**C. `pberkel/caddy-storage-redis`.** Implements the full storage interface
with `redislock` for distributed locks, supports standalone, Sentinel and
Cluster, TLS to Redis, optional AES encryption of stored values with a
32-character key, and a repair command for its index. Actively maintained
(last release 2026-08) and tested against Caddy 2.11. Requires building Caddy
with `xcaddy`. Redis must be run with persistence (AOF or RDB) and access
control.

**D. `gamalan/caddy-tlsredis`** (the module named in the original issue).
Archived in 2024; `pberkel/caddy-storage-redis` is its maintained successor
and offers a migration path through `caddy storage export` and `import`.

**E. PostgreSQL storage module (`yroc92/postgres-storage`).** Would keep
everything in the database the service already runs. Last commit 2023,
small user base, and it would put private keys next to application data
inside the same credentials boundary. Rejected.

**F. Object storage (S3-compatible).** No atomic lock primitive without an
extra coordination service. Rejected for the MVP.

## Decision

1. **The database stays the only source of truth for application and domain
   state.** Caddy's configuration is derived from it by the reconciler
   (`app/edge/`) and is never persisted as truth. The Redis module does not
   hold application state.
2. **Certificates, keys and locks live in Caddy's storage, never in the
   application database.** The application never reads or writes private
   keys.
3. **Single-instance deployments keep file storage** (`CADDY_STORAGE=file`)
   in the `https_data` volume. It is the default and needs no extra service.
4. **Deployments with more than one edge instance use the Redis storage
   module** (`CADDY_STORAGE=redis`, option C). The container image builds
   Caddy with the module. Production guidance (#12) requires it whenever
   edges are replicated, together with Redis AUTH, TLS to Redis, AOF
   persistence and a `CADDY_REDIS_ENCRYPTION_KEY`.
5. **Option D is not used** because it is archived; option C is its
   replacement.

## Consequences

- Restarting or replacing an edge does not lose registrations (database) or
  certificates (file volume or Redis); the reconciler rebuilds the Caddy
  configuration on start.
- Two edges sharing Redis take the same per-hostname lock, so only one talks
  to the CA for a given certificate; the other reads the result.
- A rejected or failed configuration update leaves the database untouched
  and Caddy on its last good configuration; the next reconciliation retries.
- Operators must back up two things: the database and the certificate store.
  Procedures are in [operations.md](../operations.md).
- The Caddy admin API must stay bound to localhost inside the container; the
  derived configuration includes the Redis password when Redis is used.
- Rotating `CADDY_REDIS_ENCRYPTION_KEY` requires `caddy storage export` with
  the old key and `import` with the new one.
- Building Caddy with `xcaddy` lengthens the image build by a few minutes and
  pins the module to a tested Caddy version.
