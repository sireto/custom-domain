# Operator portal

The portal is the `custom-domain` command in a browser: every action the
command offers (applications, origins, credentials, domains, the edge, the
doctor, the legacy import) is on a page under `/portal` of the management API,
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

**Through the edge, from allowed addresses.** Set `EDGE_HOSTNAME` (the
edge's own name, pointed at the server) and `PORTAL_ALLOWED_IPS` (your
addresses or networks, IPv4 and IPv6, comma separated, for example
`203.0.113.9,198.51.100.0/24`), and restart the API and worker. The edge
then serves `https://<EDGE_HOSTNAME>/portal`, and the same on every
application's CNAME target, with the allowlist enforced by Caddy on the
real TCP peer address: anyone else gets a 403 there, and customer hostnames
are unaffected. The certificate for the edge's name is obtained on the
first HTTPS request, so the very first visit may take a moment. Without
`EDGE_HOSTNAME`, the edge has no name of its own until the first
application exists, and the portal is reachable only over the tunnel until
then. The API checks the
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
- Secrets (API keys, webhook signing secrets) are shown once in the
  response to the action that created them and never stored in the session
  or logged. Pages are served with `Cache-Control: no-store`, a strict
  Content-Security-Policy and `X-Frame-Options: DENY`.
- Post-login redirects only go to portal paths, and the confirmation
  messages after an action are fixed texts chosen by a code, so a crafted
  link cannot put its own words on a portal page.
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

The sidebar has five sections. Every page says what it is for and what to do
next; statuses are shown in words (for example "Waiting for DNS" or "Needs
attention") with the reason next to them.

| Page | What it is for | Command-line equivalent |
|---|---|---|
| Overview | counts of live, waiting and failing hostnames; what needs attention; recent activity; purging deleted hostnames and applications past retention | `domain purge-tombstones` |
| Applications | every application with its origin and hostname counts; creating one | `application list`, `application create` |
| Application → Overview | the setup checklist (CNAME target in DNS, origin, verification, API key, first hostname, first live hostname), with the next step highlighted | |
| Application → Domains | customer hostnames, filtered by status and searched by hostname or workspace; registering one | |
| Domain | the two DNS records for the customer, each marked found, not found or wrong (with what DNS returns); the four checks; the history; check now, issue new records, delete | |
| Application → Origins | the backend traffic goes to; the token to serve and the exact URL while it is unverified; verify, activate, retire, delete | `origin register`, `origin verify --activate`, `origin activate`, `origin retire`, `origin delete` |
| Application → API keys | issue (shown once), rotate with a 24-hour overlap, revoke, delete revoked or expired keys | `credential issue`, `credential rotate`, `credential revoke`, `credential delete` |
| Application → Webhooks | endpoints with their events, signing secrets (shown once), rotation, revocation, deletion, and each endpoint's deliveries with replay | the v1 API's webhook endpoints |
| Application → Settings | name, CNAME target (optionally moving existing hostnames), the readiness check, suspend or resume, delete | `application rename`, `application set-cname-target`, `application set-workspace-probe`, `application delete` |
| Edge & DNS | every name the edge answers for, why, and what public DNS returns for it; **Verify reachability** connects to each address as a customer would; the routing summary; applying the configuration now | `edge reconcile` |
| Health checks | the doctor's findings, problems first | `doctor` |
| Import | the legacy import, with a dry run first | `legacy import` |

Deleting is always a second click, and deleting an application also asks
for its slug; while it still has live hostnames the deletion is refused
unless you confirm that they go too. A deleted application's hostnames are
tombstoned like any deleted hostname: their records and history are kept for
90 days and then purged with the application (see
[data-model.md](data-model.md#deletion-tombstones-and-retention)). An active origin cannot be deleted
(retire it first), nor can an API key or webhook that still works (revoke it
first).

An origin's verification token is shown on the Origins page for as long as
the origin is unverified: it is not a secret (the origin serves it publicly)
and the operator needs it to finish the setup. API keys and webhook signing
secrets are shown once only.

Registering a domain from the portal is for operators (tests, migrations);
applications register their customers' hostnames through the API or SDK.
