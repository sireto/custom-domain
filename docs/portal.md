# Operator portal

The portal is the `custom-domain` command in a browser: every action the
command offers (applications, origins, credentials, domains, the edge, the
doctor, the legacy import) is a page under `/portal` of the management API,
calling the same service functions, so the two never diverge. It is meant
for the operator of a self-hosted deployment; applications keep using the
v1 API with their own credentials.

## Enabling it

Set `PORTAL_PASSWORD` (at least 12 characters; `deploy/install.sh`
generates a 32-character one into `deploy/.env`) and restart the API. Until
a password is set, every portal page answers 503 with that instruction.
Sessions are signed with a key derived from the password; set
`PORTAL_SESSION_SECRET` to decouple the two, for example when rotating the
password without signing everyone out.

## Reaching it

Two ways, both on by default where they apply.

**Through the edge, from allowed addresses.** Set `PORTAL_ALLOWED_IPS` to
your addresses or networks (IPv4 and IPv6, comma separated, for example
`203.0.113.9,198.51.100.0/24`) and restart the API and worker. The edge
then serves `https://<edge name>/portal` on every application's CNAME
target (it already holds a certificate for those names), with the
allowlist enforced by Caddy on the real TCP peer address: anyone else gets
a 403 there, and customer hostnames are unaffected. The API checks the
same allowlist a second time on the address the edge forwards, and its
sign-in rate limit is per forwarded address. Nothing new is opened: port
443 is already reachable. The installer fills `PORTAL_ALLOWED_IPS` with
the address you installed from over SSH, so a fresh server is reachable
from your machine immediately. To change it, edit the value and restart the
API and worker (`docker compose -f compose.production.yml up -d api worker`);
the edge picks the new routes up on the next reconciliation, within the
reconcile interval, without a restart. The setting lives with the API: the
edge container does not need a copy.

**Through an SSH tunnel.** The production layout also publishes the API on
`127.0.0.1:9000` of the host, so
`ssh -N -L 9000:127.0.0.1:9000 root@<server>` then http://localhost:9000/portal
always works, whatever the allowlist says: private and loopback addresses
are always accepted. Leave `PORTAL_ALLOWED_IPS` empty to have the tunnel
as the only way in.

The password remains the authentication in both cases; the allowlist
limits who can even try it.

## What it protects against

- One shared operator password, compared in constant time; five failed
  sign-ins from one address block that address for fifteen minutes.
- A signed, expiring (12 hours) `HttpOnly`, `SameSite=Strict` session
  cookie scoped to `/portal`; a CSRF token bound to the session on every
  form, checked on every action.
- Secrets (credentials, origin verification tokens) are shown once in the
  response to the action that created them and never stored in the session
  or logged. Pages are served with `Cache-Control: no-store`, a strict
  Content-Security-Policy and `X-Frame-Options: DENY`.
- Post-login redirects only go to portal paths.
- Through the edge, the allowlist is applied twice: by Caddy's `remote_ip`
  matcher on the real peer (Caddy ignores a client's own `X-Forwarded-For`
  since no trusted proxies are configured), and by the API on the address
  the edge forwards, which it trusts only from the edge's own address
  range (`EDGE_ASK_TRUSTED_HOSTS`). The configuration gateway accepts the
  portal routes only in the exact shape the reconciler emits, on the edge
  names and with the allowlist the API states, and only while an allowlist
  is configured. The portal route can only ever proxy to the API itself, so
  the gateway's guarantee (no other upstreams, no file serving, no other
  listeners) is unchanged.

## Pages

| Page | Command-line equivalent |
|---|---|
| Dashboard | domain counts, last reconciliation, `domain purge-tombstones` |
| Applications | `application create`, `application list` |
| Application | `application set-cname-target` (with re-issue), `application set-workspace-probe`, suspend or activate; `origin register`, `origin verify --activate`, retire; `credential issue`, `credential rotate`, `credential revoke`; register, recheck, re-issue and delete domains |
| Domain | checks with diagnostics, the DNS records the customer publishes, the event history |
| Edge | the desired configuration summary, `edge reconcile` |
| Doctor | `doctor` |
| Legacy import | `legacy import` with dry run, reference map, grandfathering and partial imports |

Registering a domain from the portal is for operators (tests, migrations);
applications register their customers' hostnames through the API or SDK.
