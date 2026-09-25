# DNS instructions and verification

Status: implemented for issue #6. Builds on the claim model (#4) and the API
(#1); certificate and origin checks that complete readiness are #7 and #9.

## Records the customer publishes

`POST /v1/domains` returns two records with `name`, `type`, `value`,
`purpose` and `help`:

| Purpose | Record | Proves |
| --- | --- | --- |
| `ownership` | `TXT _custom-domain-challenge.<hostname>` = `custom-domain-verify=<token>` | The registrant controls the hostname's zone. The token is 256 bits from `secrets.token_urlsafe`, unique per registration, and never reused. |
| `routing` | `CNAME <hostname>` = application CNAME target | Traffic for the hostname reaches this edge. The target is per application, so instructions stay stable for an application while the edge can move. |

Ownership and routing are separate checks with separate diagnostics: a
customer can prove ownership without routing yet (for example before a
cutover), and routing without ownership never serves.

## The worker

The DNS worker runs inside the API process (`DNS_WORKER_ENABLED`, default
true) every `DNS_WORKER_INTERVAL` seconds (default 10). It selects domains
whose ownership or routing check is due, takes a two-minute lease on those
check rows so several instances never check the same domain twice, queries
DNS outside any transaction, and applies the results in a second short
transaction. A domain deleted while its queries were in flight is left
untouched.

Queries go to the system resolvers or `DNS_RESOLVERS` (comma-separated IPs)
with `DNS_TIMEOUT` seconds per lookup. CNAME chains are followed for up to 8
hops and loops are cut. NXDOMAIN, missing records, timeouts and SERVFAIL are
distinguished.

Scheduling:

| Situation | Next attempt |
| --- | --- |
| Check failing or pending | 1, 2, 5, 15, 30 minutes, then hourly (bounded backoff per check, `attempts` and `failing_since` kept in the check's details) |
| Check passing | every 6 hours (revalidation) |
| Manual recheck (`POST /v1/domains/{id}/checks`) | now, subject to the API rate limit |

`custom-domain checks run` processes due checks once; `custom-domain checks
dns --application acme --hostname forms.customer.example` runs both checks
for one domain immediately and prints the diagnostics.

## Diagnostics

| Check | `error_code` | Meaning |
| --- | --- | --- |
| ownership | `txt_record_not_found` | The name does not exist or has no TXT record. |
| ownership | `txt_token_mismatch` | TXT records exist but none is this registration's value (`details.observed` lists them). |
| ownership | `txt_token_stale` | The TXT holds a token from an earlier claim of this domain (re-issued instructions or a previous registration). |
| ownership | `claim_revoked` | No live claim; the domain is deleted or being re-issued. |
| routing | `cname_not_found` | No CNAME: the name is missing, or has A/AAAA records instead (the message says which). |
| routing | `cname_target_mismatch` | The CNAME chain ends elsewhere (`details.chain` shows it). |
| either | `dns_timeout` | The resolver did not answer; retried with backoff. |

## Status rules

| From | Both pass | Either fails |
| --- | --- | --- |
| `pending_dns` | claim verified (`dns_txt`), then `provisioning` | stays, backoff |
| `provisioning` | stays; the certificate and origin checks ([tls-readiness.md](tls-readiness.md)) then move it to `ready` | `pending_dns` |
| `ready` | stays, revalidated every 6 hours | `attention_required` immediately; serving stops |
| `attention_required` | `ready` again when the other checks still pass (`domain.recovered`) | stays; if ownership has failed for 24 hours on a verified claim, `suspended` |
| `suspended` | `provisioning` (the domain must complete readiness again) | stays |

Serving requires `ready`, so drift stops traffic on the next revalidation or
manual recheck at the latest. Loss of ownership is treated more severely
than loss of routing: after the grace period the domain is suspended and must
pass both checks again before it can return to service.

## Takeover resistance

- Tokens are per registration and never reused. Re-issuing instructions
  revokes the old claim, resets every check and moves the domain back to
  `pending_dns`; the old token is then reported as `txt_token_stale`.
- A hostname deleted by one application and registered by another gets a
  new domain row and a new token. Records left behind by the previous owner
  cannot verify the new claim (they report `txt_token_mismatch` and
  `cname_target_mismatch`), and the previous registration is a tombstone that
  is never checked or served.
- Only one live claim per hostname exists across applications (database
  constraint from #4), so two registrants cannot both be verified.
- Manual rechecks are rate limited per domain and per application (#1).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DNS_WORKER_ENABLED` | `true` | Run the worker in this process. |
| `DNS_WORKER_INTERVAL` | `10` | Seconds between batches. |
| `DNS_WORKER_BATCH` | `50` | Domains per batch. |
| `DNS_RESOLVERS` | system | Nameserver IPs to query. |
| `DNS_TIMEOUT` | `5` | Seconds per lookup. |
| `DNS_VERIFICATION_MODE` | `public` | `local` answers the ownership and routing checks from the records the service issued (see below). |

### Local mode

`DNS_VERIFICATION_MODE=local` replaces the resolver with one that answers
each check from the service's own records: every live domain is treated as
if its customer had published the TXT and CNAME records exactly as
instructed. Everything else is unchanged, so a local instance runs the real
lifecycle (verification, certificate, workspace probe, webhooks) for
invented hostnames. Revoked claims are not answered, so a re-issued claim
still resets the domain. The mode is refused unless the edge is local as
well (`DISABLE_HTTPS=true` or `EDGE_TLS_ISSUER=internal`): a publicly
trusted edge must verify real DNS. `deploy/compose.local.yml` uses it.
