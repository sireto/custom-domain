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

The management API is not published to the internet by design. In the
production layout it listens on `127.0.0.1:9000` of the host, so an SSH
tunnel is all it takes:

```
ssh -N -L 9000:127.0.0.1:9000 root@<server>
```

then open http://localhost:9000/portal. The single-container and local
layouts publish the same port. A reverse proxy of your own with TLS in
front of port 9000 works too; the portal sets `Secure` on its cookie when
it is reached over HTTPS and is otherwise safe to expose only where the
password alone is an acceptable barrier, since the password is the whole
authentication.

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
