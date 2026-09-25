# Domain lifecycle

Status: implemented for issue #9. Consolidates the rules from DNS
verification (#6), certificates and readiness (#7) and routing (#8).

## Checks

Each domain carries four checks, each `pending`, `passing` or `failing` with
`error_code`, `message`, `details`, `observed_at` and `next_check_at`:

| Check | Proves | Detail |
| --- | --- | --- |
| `ownership` | The registrant controls the hostname's zone. | TXT record from the live claim ([dns-verification.md](dns-verification.md)) |
| `routing` | Traffic for the hostname reaches this edge. | CNAME chain through the application's target |
| `certificate` | A valid certificate is served for the hostname by this edge. | Readiness probe ([tls-readiness.md](tls-readiness.md)) |
| `origin` | Requests reach the origin and select the correct workspace. | Verified active origin plus the workspace probe below |

All checks run in the checks worker, never inside an API request or a TLS
handshake. Failing or pending checks retry at 1, 2, 5, 15, 30 minutes then
hourly; passing checks are revalidated every 6 hours. `attempts` and
`failing_since` in the details support a customer-facing status page
together with the error codes and timestamps.

## The workspace probe

The `origin` check is the "HTTPS to the correct workspace" probe. Through the
edge, with the hostname as SNI and a fresh assertion (#8), the worker
requests

```
GET https://<hostname>/.well-known/custom-domain-workspace
```

and the origin must answer `200 application/json`:

```json
{"reference": "<the ref from the verified assertion>", "application": "<the app from it>"}
```

`application` is optional but checked when present. The Python SDK (#11)
provides this handler. The check passes only when `reference` equals the
domain's registered workspace reference, which proves routing, assertion
verification and tenant selection end to end. Failures:

| `error_code` | Meaning |
| --- | --- |
| `origin_not_ready` | The application has no verified active origin. |
| `workspace_probe_failed` | The path did not return 200 (origin not deployed with the handler, or a proxy intercepts it). |
| `workspace_probe_invalid` | 200 without a JSON `reference`. |
| `workspace_mismatch` | The origin selected another workspace: it is not deriving the tenant from the assertion. Serving is not allowed until fixed. |
| transport codes | Same as the certificate probe (`connection_failed`, `timeout`, `tls_*`, `edge_unresolvable`). |

An application that cannot implement the handler can be exempted with
`custom-domain application set-workspace-probe --application acme --disabled`;
its `origin` check then only requires a verified active origin, and
readiness no longer proves workspace selection. Use it for staging only.

## Status machine

| Status | Meaning | Serves |
| --- | --- | --- |
| `pending_dns` | Waiting for ownership and routing records. | no |
| `provisioning` | Ownership verified; certificate and origin checks running. | no |
| `ready` | All four checks pass, claim verified. | yes |
| `attention_required` | A check that passed is failing again. | no |
| `suspended` | Ownership lost for 24 hours, or suspended by policy. | no |
| `deleting` | Tombstone. | no |

Transitions the worker performs:

| From | Event | To | Event reason |
| --- | --- | --- | --- |
| `pending_dns` | ownership and routing pass | `provisioning` | `dns_verified` |
| `provisioning` | ownership or routing fails | `pending_dns` | `<check>_check_failed` |
| `provisioning` | all four checks pass | `ready` | `readiness_probe_passed` |
| `ready` | any check fails | `attention_required` | `<check>_check_failed` |
| `attention_required` | all four checks pass | `ready` | `recovered` |
| `attention_required` | ownership failing 24 h on a verified claim | `suspended` | `ownership_lost` |
| `suspended` | ownership and routing pass | `provisioning` | `dns_verified` |
| any but `deleting` | `DELETE /v1/domains/{id}` | `deleting` | tombstone |

Traffic policy: only `ready` domains are routed, and every request is
re-checked at the edge's assert step, so a transition out of `ready`, a
suspension or a deletion stops traffic on the next request. Certificate
renewal continues in `attention_required` and stops in `suspended`.

Each transition is recorded as a `domain.status_changed` event with the
reason; check updates are `domain.check_updated` events. Webhooks (#10) map
`ready` entries to `domain.ready` or `domain.recovered`, `attention_required`
entries to `domain.attention_required`, and deletions to `domain.deleted`.

## Idempotency, restarts and races

- Due checks are claimed per domain with a two-minute lease on the check
  rows. Two workers cannot process the same domain at once; if a worker
  crashes mid-run the lease simply expires and the domain is due again.
- Network work runs outside transactions; results are applied under the
  application lock and re-read the domain first, so a domain deleted or
  changed during the queries is not written to (`process_domain` returns
  without applying).
- Re-running a check is idempotent: passing again does not repeat
  transitions, and the event stream records what actually changed.
- Reconciliation of the edge (#2) is triggered right after a batch in which
  a status changed, in addition to its timer.

## Running the worker

By default the checks worker and the reconciler run inside the API process
(`DNS_WORKER_ENABLED`, `EDGE_RECONCILE_ENABLED`). To run them separately:

```
DNS_WORKER_ENABLED=false EDGE_RECONCILE_ENABLED=false   # in the API containers
custom-domain worker run                                # in one worker container
custom-domain worker run --once                         # one pass, for cron or debugging
```

Several worker processes may run at once; leases and the reconcile lock keep
them from interfering.
