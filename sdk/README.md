# custom-domain-sdk

Python client and origin helpers for the Custom Domain API. Pure Python 3.10+
with `httpx`; no dependency on the service itself.

```
pip install custom-domain-sdk        # once published; from this repo: pip install ./sdk
```

## Quick start: register a workspace domain

```python
from custom_domain import Client

client = Client("https://domains.example.net", credential="cd_...")  # credential from the operator

domain = client.create_domain(
    "forms.customer.example",  # the customer's exact subdomain
    reference="ws_8f3a1c",  # your workspace id, returned to you on every request
    idempotency_key="signup-ws_8f3a1c",  # makes the call safe to retry
)
print(domain.status)  # pending_dns
print(domain.render_dns_instructions())
```

prints something like

```
Create these DNS records for forms.customer.example:

TXT   _custom-domain-challenge.forms.customer.example  ->  custom-domain-verify=...
      purpose: ownership. Create a TXT record with exactly this name and value. ...
CNAME forms.customer.example  ->  acme.edge.example.net
      purpose: routing. Point the hostname at the target with a CNAME record. ...
```

Show both records to the customer as returned; each carries `help` text for
the usual DNS console mistakes. Nothing here requires knowing anything about
the edge or Caddy.

Later:

```python
domain = client.get_domain(domain.id)
for check in domain.checks:
    print(check.type, check.status, check.error_code)
if not domain.is_ready:
    client.request_recheck(domain.id)  # after the customer fixed DNS; rate limited
for d in client.iter_domains(reference="ws_8f3a1c"):
    ...
client.delete_domain(domain.id)
```

## Errors

Every API error raises a subclass of `custom_domain.ApiError` with `status`,
the stable `code` from the API, `message` and `details`:
`AuthenticationError` (401), `NotFoundError` (404), `ConflictError` (409, for
example `hostname_already_claimed`), `ValidationError` (422, for example
`apex_not_supported`), `RateLimitedError` (429, with `retry_after`),
`ServerError` (5xx). `TransportError` means no HTTP response was received.

## Timeouts and retries

Each request has a 10 second timeout (`Client(timeout=...)`). Requests that
are safe to repeat, that is `GET`, `DELETE`, and `create_domain` **when an
idempotency key is given**, are retried up to `max_retries` (default 2) on
connection errors, timeouts, 429 (waiting `Retry-After`) and 5xx, with
exponential backoff. A create without an idempotency key and a recheck are
never retried by the client: retrying a create could register twice and a
recheck is rate limited, so the caller decides.

## Verifying requests at your origin

The edge adds `X-Custom-Domain-Assertion` to every proxied request. Select
the workspace from it and from nothing else.

ASGI (FastAPI, Starlette, and others):

```python
from custom_domain import CustomDomainMiddleware

app.add_middleware(
    CustomDomainMiddleware,
    keys={
        "1": "<EDGE_ASSERTION_KEYS secret from the operator>"
    },  # current and previous key during rotation
    application_id="<your application id>",
    on_missing="reject",  # "passthrough" if this app also serves its own domain
)


@app.get("/")
def home(request: Request):
    assertion = request.state.custom_domain  # None only with on_missing="passthrough"
    workspace = load_workspace(assertion.reference)
```

The middleware also answers `GET /.well-known/custom-domain-workspace` with the
verified reference, which the service's lifecycle worker uses to prove that
routing and tenant selection work before it marks a domain ready.

Any framework:

```python
from custom_domain import WorkspaceResolver, AssertionInvalid

resolver = WorkspaceResolver(keys={"1": "..."}, application_id="...")
try:
    assertion = resolver.resolve(request.headers, request.host)
except AssertionInvalid as exc:
    return forbidden(exc.code)  # missing, expired, bad_signature, wrong_application, ...
```

Origins should only be reachable from the edge (network rules), or treat any
request without a valid assertion as a direct call and refuse it.

## Webhooks

```python
from custom_domain import verify_webhook, parse_event, SignatureInvalid

hook = client.create_webhook(
    "https://app.acme.example/hooks/custom-domain", ["domain.ready", "domain.deleted"]
)
store(hook.secret)  # shown once

# in your handler
try:
    verify_webhook(
        request.headers.get("X-Custom-Domain-Signature"), body, secrets=[current, previous]
    )
except SignatureInvalid:
    return 400
event = parse_event(body)  # event.id, event.type, event.created_at, event.domain
```

Deliveries are at least once and may arrive out of order: deduplicate by
`event.id` and compare `event.created_at` before moving state. A complete
consumer is in `examples/webhook_consumer.py`.

## Local development versus production

Against the local Compose environment (`DISABLE_HTTPS=true`,
`ORIGIN_ALLOW_PRIVATE=true`), the edge serves plain HTTP, the certificate
check is marked not applicable, and origins on private addresses are
accepted. In production the edge obtains real certificates on demand at the
first handshake after DNS points to it, verifies origin TLS, refuses private
origin and webhook addresses, and a domain becomes `ready` only after the
HTTPS probe through the edge returns the right workspace. Customer DNS
changes take up to the record TTL to be observed; the API's `checks` show the
last observation and the next attempt.

## Compatibility

The SDK follows the API's `v1` contract. Minor SDK releases add fields and
methods; a field the API stops sending is never removed within a major
version. Unknown fields in API responses are ignored, so newer servers work
with older SDKs. Python 3.10 and newer are supported.
