# Operator API

The operator actions of the `custom-domain` command, over HTTP, for programs:
for example a control plane that creates an application for each of its
customers on a shared deployment, or a script that provisions a new tenant.
Every endpoint calls the same service function as the command and the
[portal](portal.md), so the three never diverge.

It covers what the [v1 API](api-v1.md) cannot do. v1 is scoped to one
application by its credential; the operator API manages the applications
themselves, their origins and their credentials. For an application's
domains and webhooks, issue it a credential here and use v1 with that
credential.

## Enabling it

Set `OPERATOR_API_TOKEN` (at least 32 random characters, for example
`openssl rand -hex 32`) in `deploy/.env` and restart the api and worker.
Without it, or with a shorter value, every operator path answers 404.

For an automated install, give `OPERATOR_API_TOKEN` and
`OPERATOR_ALLOWED_IPS` to the installer, in the environment or in the
cloud-init's `/etc/custom-domain-install.env`. It writes them to `.env`, and
on an upgrade replaces them only when they are given again. The installer
refuses a token shorter than 32 characters or containing characters other
than letters, digits and `. _ ~ + / = -`, and never prints it.

Callers send it as `Authorization: Bearer <token>`. The token has the same
power as the `custom-domain` command: keep it in a secret store and rotate it
by changing the value and restarting.

## Rotating the token

`PUT /operator/v1/token` with `{"token": "<new token>"}`, authenticated with
the current token, replaces it without editing `.env` or restarting.

- **Every instance at once:** only the new token's SHA-256 is stored, in
  the database, so every API instance accepts the new token immediately.
- **The old token stops working,** including `OPERATOR_API_TOKEN`. This
  retires a token handed out at install time, for example in cloud-init
  user data, which a cloud's metadata service keeps serving for the
  server's whole life.
- **The new token** must be 32 to 256 characters of letters, digits and
  `. _ ~ + / = -`.
- **Logging:** neither token is ever logged.
- **On the host:** `custom-domain operator token-status` says which token
  is in force. `custom-domain operator reset-token` forgets the rotated
  one, so `OPERATOR_API_TOKEN` works again, if the new token is lost.

## Reaching it

- **On the API's own port** (`127.0.0.1:9000` of the host in the production
  layout, or the Docker network): from private and loopback addresses, always.
- **Through the edge**, at `https://<edge name>/operator/v1`, from the
  addresses in `OPERATOR_ALLOWED_IPS` only (addresses or CIDR networks,
  comma separated, never the whole address space). Caddy enforces the list on
  the real peer and answers 403 to everyone else; the API checks the address
  the edge forwards again. The configuration gateway accepts these routes only
  in the reconciler's exact shape, on the edge's own names, and only while the
  API states the allowlist. Customer hostnames never expose them, and the
  public `/v1` route never includes them.

Failed tokens are throttled per client like v1 (`V1_AUTH_FAILURES_PER_MINUTE`).

**If you run your own reverse proxy in front of port 9000** (the
`PUBLIC_API=false` setup in [deployment.md](deployment.md#the-v1-api-through-the-edge)),
do not forward `/operator` through it, or restrict it there to your own
addresses. To the API every request from the proxy comes from the proxy's
private address, which is always admitted and never throttled, so
`OPERATOR_ALLOWED_IPS` and the throttling do not apply and the token would be
the only barrier. Serve `/operator` through the edge instead, where the
allowlist is enforced on the real client.

## Endpoints

All paths are under `/operator/v1`; errors use the v1 envelope
`{"error": {"code", "message", "details"}}`. The contract is in
[openapi.json](openapi.json) under the `Operator` tag.

| Method and path | Does | Command equivalent |
|---|---|---|
| `PUT /token` `{token}` | replace the operator token (see above) | `operator reset-token` undoes it |
| `GET /backup` | download `pg_dump --format=custom` of the database (PostgreSQL only) | `pg_dump`, see [operations.md](operations.md#backup) |
| `GET /applications` | list | `application list` |
| `POST /applications` `{slug, name, cname_target?}` | create (`cname_target` defaults to `EDGE_HOSTNAME`) | `application create` |
| `GET /applications/{slug}` | read; includes `max_domains` (the limit, or null) and `live_domains` | |
| `PATCH /applications/{slug}` `{name?, cname_target?, reissue_claims?, workspace_probe?, status?, max_domains?}` | change; `max_domains: null` removes the limit, omitting it leaves it unchanged | `application rename`, `set-cname-target`, `set-workspace-probe`, `set-domain-limit`, suspend or resume |
| `DELETE /applications/{slug}?confirm={slug}&delete_domains=true` | delete (domains kept 90 days as tombstones) | `application delete` |
| `GET /applications/{slug}/origins` | list | `origin list` |
| `POST /applications/{slug}/origins` `{host, scheme?, port?}` | register; returns `verification_token` and `verification_url` | `origin register` |
| `POST /applications/{slug}/origins/{id}/verify` `{activate?}` | verify (and by default activate); `422 origin_verification_failed` says why not | `origin verify --activate` |
| `POST /applications/{slug}/origins/{id}/activate`, `/retire` | route traffic to it, or stop | `origin activate`, `origin retire` |
| `DELETE /applications/{slug}/origins/{id}` | delete one that carries no traffic | `origin delete` |
| `GET /applications/{slug}/credentials` | list (never the keys) | `credential list` |
| `POST /applications/{slug}/credentials` `{label, expires_in_days?}` | issue; the response carries `secret` once | `credential issue` |
| `POST /applications/{slug}/credentials/{id}/rotate` `{grace_hours?}` | replace, the old one expiring after the overlap | `credential rotate` |
| `POST /applications/{slug}/credentials/{id}/revoke` | revoke | `credential revoke` |
| `DELETE /applications/{slug}/credentials/{id}` | delete a revoked or expired one | `credential delete` |
| `GET /doctor` | the doctor's findings, for monitoring | `doctor` |

## Provisioning a tenant

```
T="Authorization: Bearer $OPERATOR_API_TOKEN"
API=https://edge.example.net/operator/v1

curl -s -H "$T" -X POST $API/applications -d '{"slug":"acme","name":"Acme"}' -H 'Content-Type: application/json'
curl -s -H "$T" -X POST $API/applications/acme/origins -d '{"host":"app.acme.example"}' -H 'Content-Type: application/json'
# serve the returned verification_token at verification_url, then:
curl -s -H "$T" -X POST $API/applications/acme/origins/<id>/verify
curl -s -H "$T" -X POST $API/applications/acme/credentials -d '{"label":"backend"}' -H 'Content-Type: application/json'
# the tenant's backend now uses https://edge.example.net/v1 with that secret
```
