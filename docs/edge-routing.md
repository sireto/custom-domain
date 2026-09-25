# Edge routing and the signed workspace assertion

Status: implemented for issue #8. Builds on the reconciler (#2), origin
proof (#5) and readiness (#6, #7).

## What happens on a request

1. TLS: Caddy selects the certificate for the SNI and, with
   `strict_sni_host`, refuses any request whose `Host` differs from the SNI
   (HTTP 421). Certificates exist only for authorized hostnames (#7).
2. Route: the derived configuration has one route per application whose
   host matcher lists that application's routable hostnames: live, claim
   verified, application active with a verified active origin, status
   `provisioning`, `ready` or `attention_required`. Routing is wider than
   serving on purpose: before a domain is `ready` the assert step below
   admits only the workspace probe path, so the lifecycle worker can prove
   tenant selection through the edge. There is no catch-all proxy: a
   hostname that matches no application route hits a terminal 404 route,
   never an origin.
3. Strip: every `X-Custom-Domain-*` header the client sent is deleted.
4. Assert: Caddy makes a subrequest to `GET /internal/edge/assert` on the
   management API with the request's `Host`, TLS SNI and request id. The
   API canonicalizes the hostname, refuses a `Host`/SNI mismatch, looks up
   the single live domain row by hostname (indexed), re-checks that it is
   serveable now and that its application has a serving origin, and returns
   `200` with `X-Custom-Domain-Assertion`. Anything else is `403`, which
   Caddy returns to the client; nothing reaches an origin without an
   assertion. This lookup happens per request, so a deletion or suspension
   stops traffic immediately, before the reconciler's next tick.
5. Proxy: the request goes to the application's active origin with the
   assertion header, `Host` set to the customer-facing hostname,
   `X-Forwarded-Host` the same, `X-Forwarded-Proto` the scheme the client
   used and Caddy's `X-Forwarded-For`. For HTTPS origins the upstream TLS
   handshake uses the origin's own name as SNI and verifies its certificate.

## The assertion

```
X-Custom-Domain-Assertion: v1.<key id>.<base64url payload>.<base64url HMAC-SHA256>
```

Payload (compact JSON):

| Field | Meaning |
| --- | --- |
| `app` | Application id the edge routed for. Origins must compare it to their own id. |
| `dom` | Domain id. |
| `ref` | The workspace reference registered with the domain. This is the value to select the tenant by. |
| `host` | Canonical hostname. |
| `iat`, `exp` | Issue and expiry, Unix seconds. TTL is `EDGE_ASSERTION_TTL` (60 s default). |
| `rid` | Caddy's request id. |

The MAC is HMAC-SHA256 with the key named by `<key id>` over
`v1.<key id>.<payload>`.

## Verifying at the origin

The origin must, for every request it treats as coming through a custom
domain:

1. Read `X-Custom-Domain-Assertion`; reject the request if absent.
2. Split on `.`; require four parts and `v1`.
3. Look up the key by id; reject unknown ids.
4. Recompute the MAC over the first three parts and compare in constant
   time.
5. Decode the payload; reject if `exp + skew < now` or `iat > now + skew`
   (30 s skew).
6. Require `app` to equal the origin's own application id (given by the
   operator), and `host` to equal the request's `Host`.
7. Only then use `ref` to select the workspace. Never read the workspace
   from `Host`, a query parameter or any other header.

`app.edge.assertion.verify()` implements exactly this and the Python SDK
(#11) ships it for origins. Origins must also serve `GET /.well-known/custom-domain-workspace`
with the reference from the verified assertion; the lifecycle worker uses it to
prove correct workspace selection before a domain becomes ready
([lifecycle.md](lifecycle.md)). Origins that receive requests on their own
domain as well should apply the check only when the header is present and
serve their own domain otherwise, or, better, restrict the custom-domain
listener to the edge (see below).

## Keys and rotation

`EDGE_ASSERTION_KEYS` is a comma-separated list of `<key id>:<secret>`
entries with secrets of at least 32 characters. The first entry signs; every
entry verifies. To rotate: add the new key first in the API's list and
deploy (it now signs with the new key); give origins both keys; after
`EDGE_ASSERTION_TTL` plus clock skew, remove the old key from origins, then
from the API. The signing key is separate from application credentials and
from the webhook secret (#10), so compromise of one does not expose the
others. Without a configured key the assert endpoint answers `503` and
nothing is routed.

## Replay and bypass

An assertion is bound to one hostname, application, domain and reference,
lives for 60 seconds, and carries a request id. Replaying it within its
lifetime only lets an attacker repeat a request as the same workspace on the
same hostname, and only if they can reach the origin directly. That is why
origins must not be reachable except from the edge:

- put the origin on a private network, firewall or security group that
  admits only the edge's addresses, or terminate a mutual-TLS or VPN link
  between the edge and the origin (`EDGE_PROBE_ADDRESS`-style deployments);
- if the origin must be public, treat any request without a valid assertion
  as a direct call and refuse it on the custom-domain code path;
- origins that need strict single-use can keep a short cache of `rid`
  values for the TTL window.

Requests never reach an origin without a fresh assertion because the edge
strips whatever the client sent and only copies the header from a `200`
answer of the assert endpoint.

## Redirects and URLs

The origin sees the customer-facing URL: `Host` and `X-Forwarded-Host` are
the customer hostname and `X-Forwarded-Proto` is `https`, so absolute URLs,
cookies and redirects the origin builds from the request are correct without
rewriting. The edge does not rewrite `Location` headers. HTTP requests to
port 80 are redirected to HTTPS by Caddy before any of the above.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EDGE_ASSERTION_KEYS` | required when the edge is enabled | `<id>:<secret>,...`, first signs. |
| `EDGE_ASSERTION_TTL` | `60` | Assertion lifetime in seconds (5 to 600). |
| `EDGE_ASSERT_UPSTREAM` | `localhost:9000` | Where Caddy sends the assert subrequest. |
| `EDGE_ASK_TRUSTED_HOSTS` | `127.0.0.1,::1` | Client addresses allowed to call the internal endpoints. |

Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Verified behaviour

`tests/test_edge_routing.py` runs the derived configuration through a real
Caddy process against two applications with two workspaces each: requests
for each hostname reach the right origin with an assertion naming that
application and workspace, a forged client assertion and a client-supplied
reference header are stripped, `Host` and `X-Forwarded-*` are as documented,
unknown and not-yet-ready hostnames never reach an origin, and a deleted
domain is refused on the next request.
