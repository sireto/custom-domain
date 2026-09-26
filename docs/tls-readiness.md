# Certificates and HTTPS readiness

Status: implemented for issue #7. Builds on the reconciler (#2), DNS
verification (#6) and origin proof (#5).

## On-demand certificates

Caddy obtains certificates on demand: at the first TLS handshake for a
hostname, and only after asking this service whether that hostname is
eligible. The derived configuration (#2) sets

```
tls.automation.on_demand.permission = {module: http, endpoint: EDGE_ASK_URL}
tls.automation.policies = [{on_demand: true, issuers: [acme]}]
```

`EDGE_ASK_URL` defaults to `http://localhost:9000/internal/tls/ask`, the
management API inside the same container. The endpoint answers `200` (issue
or renew) or `403` (deny) from one indexed lookup of the live domain row; it
never queries DNS, so a handshake is never blocked on a resolver. It accepts
requests only from `EDGE_ASK_TRUSTED_HOSTS` (default loopback) and is not
part of the public contract.

Authorization rule (`certificate_authorized`): the hostname has a live
domain row, its ownership claim is verified, its application is active, and
its status is not `pending_dns`, `suspended` or `deleting`. Unknown,
unverified, suspended and deleted names are therefore denied; `provisioning`,
`ready` and `attention_required` names can obtain and renew.

Because issuance is on demand, no certificate exists before the first
handshake. The readiness probe below is that first handshake, so a domain
gets its certificate as part of becoming ready rather than at cutover. There
is no pre-cutover issuance in the MVP; the first real visitor after a
cutover would otherwise wait for issuance (typically a few seconds).

Certificate storage, renewal coordination across edges and key access
boundaries are in [ADR 0001](decisions/0001-certificate-storage.md) and
[operations.md](operations.md).

## Readiness probe

Once ownership and routing pass, the checks worker runs the certificate
check: it connects to the edge with the hostname as SNI, verifies the
certificate chain and hostname, then requests
`/.well-known/custom-domain-edge-health` and expects `204` with the
`X-Custom-Domain-Edge: 1` header that the derived configuration serves on
every hostname. The probe therefore proves in one step that a valid
certificate exists, that the handshake works for the hostname, and that the
address it reached is one of our edges rather than a server the customer
still points at.

By default the probe connects through the public path: it resolves the
hostname and connects to that address, so it also confirms that DNS delivers
traffic to the edge. Set `EDGE_PROBE_ADDRESS=host:port` when the container
cannot reach its own public address (hairpin NAT) and `EDGE_PROBE_CA_FILE`
when the edge uses a private CA (local development, staging). TLS
verification is never skipped.

The origin check passes when the application has a verified active origin
(#5); a per-hostname origin request is added by the lifecycle work in #9.

A `provisioning` domain becomes `ready` when ownership, routing, certificate
and origin all pass and the claim is verified. `ready` is never asserted from
DNS alone. The edge server always serves TLS (an empty connection policy),
so the first handshake for a freshly verified hostname can trigger on-demand
issuance before any application route exists for it; the reconciler adds the
route as soon as the claim is verified, and the worker reconciles between its
DNS and edge phases so the readiness probe finds it in place.

## Diagnostics

| Check | `error_code` | Meaning |
| --- | --- | --- |
| certificate | `tls_handshake_failed` | The edge could not complete the handshake, usually because issuance was denied or failed on this first attempt. Caddy's log has the ACME error (CAA restriction, rate limit, challenge failure). Retried with backoff. |
| certificate | `certificate_untrusted` | The chain does not verify against the trust store (`EDGE_PROBE_CA_FILE` for private CAs). |
| certificate | `certificate_hostname_mismatch` | The certificate served is for another name; usually another server answered. |
| certificate | `certificate_expired` / `certificate_expiring` | Expired, or less than 7 days left: renewal is failing. Check Caddy's log, CAA records and CA rate limits. |
| certificate | `edge_not_reached` | HTTPS answered but without this edge's marker: the hostname reaches another server. |
| certificate | `edge_unresolvable`, `connection_failed`, `timeout` | The probe could not reach an edge at all. |
| origin | `origin_not_ready` | The application has no verified active origin. |

Scheduling and backoff follow the DNS checks: failing checks retry at 1, 2,
5, 15, 30 minutes then hourly; passing checks are revalidated every 6 hours,
which also catches an expiring certificate well before it expires (Caddy
renews at two thirds of the lifetime, so `certificate_expiring` means
renewal has been failing for weeks).

On a `ready` domain a failing certificate or origin check moves it to
`attention_required` and serving stops; it returns to `ready` when the checks
pass again. Certificate renewal continues while a domain is
`attention_required`, because its claim is still verified.

## Observing ACME failures

Caddy logs every issuance and renewal attempt as JSON on the container's
stdout (`logger: tls.obtain` / `tls.renew`), including CAA denials, rate
limits and challenge errors. The certificate check surfaces the effect
(`tls_handshake_failed` on first issuance, `certificate_expiring` on failed
renewal) on the domain and in the API; correlating the log line to the
domain is by hostname. Structured export of Caddy's events is part of #12.

## Operator commands

```
uv run custom-domain checks run                                  # all due checks once
uv run custom-domain checks edge --application acme --hostname forms.customer.example
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EDGE_ASK_URL` | `http://localhost:9000/internal/tls/ask` | Where Caddy asks for permission. |
| `EDGE_ASK_TRUSTED_HOSTS` | `127.0.0.1,::1` | Client addresses allowed to call the ask endpoint. |
| `EDGE_PROBE_ADDRESS` | resolve the hostname | Fixed `host:port` to probe instead of the public path. |
| `EDGE_PROBE_CA_FILE` | system store | CA bundle to trust for the probe. |
| `EDGE_PROBE_TIMEOUT` | `15` | Seconds for connect, handshake and request. |
| `EDGE_TLS_ISSUER` | `acme` | `internal` uses Caddy's local CA (development and staging); point `EDGE_PROBE_CA_FILE` at its root certificate. |

## The edge's own names

The ask endpoint also allows a certificate for the CNAME target of any
active application (`custom-domain application create --cname-target`).
No application route matches such a name, so the edge only answers the
health path on it; `custom-domain doctor` uses that to confirm, over
HTTPS, that the name customers CNAME to reaches this edge and that
issuance works.
