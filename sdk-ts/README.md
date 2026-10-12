# @sireto/custom-domain

TypeScript and JavaScript SDK for the Custom Domain API: a typed client,
verification of the edge's signed workspace assertion, middleware for Fetch
API frameworks and Express, and webhook verification.

- **Zero runtime dependencies.** HMAC uses Web Crypto and requests use the
  global `fetch`, so the same package runs in Node 20+, Deno, Bun, Cloudflare
  Workers and Vercel Edge.
- **The same behaviour as the Python SDK** ([`sdk/`](https://github.com/sireto/custom-domain/blob/main/sdk/README.md)),
  checked against fixtures made by the Python code.
- ESM with type declarations. The version is the service's release version.

```
npm install @sireto/custom-domain
```

## Quick start: register a workspace domain

The client holds your application's API key. Use it on your server only,
never in a browser.

```ts
import { CustomDomainClient, renderDnsInstructions } from "@sireto/custom-domain";

const client = new CustomDomainClient({
  baseUrl: "https://edge.example.net", // the API's address, from the operator
  apiKey: process.env.CUSTOM_DOMAIN_API_KEY!, // cd_..., from the operator
});

const domain = await client.createDomain(
  {
    hostname: "forms.customer.example", // the customer's exact subdomain
    reference: "ws_8f3a1c", // your workspace id, returned to you on every request
  },
  { idempotencyKey: "signup-ws_8f3a1c" }, // makes the call safe to retry
);
console.log(domain.status); // "pending_dns"
console.log(renderDnsInstructions(domain));
```

prints something like

```
Create these DNS records for forms.customer.example:

TXT   _custom-domain-challenge.forms.customer.example  ->  custom-domain-verify=...
      purpose: ownership. Create a TXT record with exactly this name and value. ...
CNAME forms.customer.example  ->  acme.edge.example.net
      purpose: routing. Point the hostname at the target with a CNAME record. ...
```

Show both records to the customer as returned (`domain.dns_records`); each
carries `help` text for the usual DNS console mistakes.

Later:

```ts
import { CustomDomainClient, isReady } from "@sireto/custom-domain";

declare const client: CustomDomainClient;
declare const domainId: string;

const domain = await client.getDomain(domainId);
for (const check of domain.checks) {
  console.log(check.type, check.status, check.error_code);
}
if (!isReady(domain)) {
  await client.recheckDomain(domain.id); // after the customer fixed DNS; rate limited
}
for await (const d of client.iterDomains({ reference: "ws_8f3a1c" })) {
  console.log(d.hostname, d.status);
}
await client.deleteDomain(domain.id);
```

Resources are the API's JSON as typed objects (generated from
[`docs/openapi.json`](https://github.com/sireto/custom-domain/blob/main/docs/openapi.json)): field names are the API's
(`dns_records`, `next_offset`) and timestamps are ISO 8601 strings.

| Method | API |
|---|---|
| `createDomain({hostname, reference, metadata}, {idempotencyKey})` | `POST /v1/domains` |
| `getDomain(id, {includeDeleted})` | `GET /v1/domains/{id}` |
| `listDomains({reference, status, includeDeleted, limit, offset})` | `GET /v1/domains` |
| `iterDomains({reference, status, includeDeleted, pageSize})` | every page of `GET /v1/domains` |
| `recheckDomain(id)` | `POST /v1/domains/{id}/checks` |
| `deleteDomain(id)` | `DELETE /v1/domains/{id}` |
| `createWebhook({url, events})` | `POST /v1/webhooks` |
| `listWebhooks()` | `GET /v1/webhooks` |
| `rotateWebhook(id)` | `POST /v1/webhooks/{id}/rotate` |
| `deleteWebhook(id)` | `DELETE /v1/webhooks/{id}` |
| `listDeliveries(id, {state, limit, offset})` | `GET /v1/webhooks/{id}/deliveries` |
| `replayDelivery(id, deliveryId)` | `POST /v1/webhooks/{id}/deliveries/{delivery_id}/replay` |
| `replaySince(id, since)` | `POST /v1/webhooks/{id}/replay?since=...` |

Every method also takes a `signal` (an `AbortSignal`) in its last argument.

## Errors

Every API error throws a subclass of `ApiError` with `status`, the stable
`code` from the API ([api-v1.md](https://github.com/sireto/custom-domain/blob/main/docs/api-v1.md#errors)), `message`,
`details` and `requestId` (when a proxy sets `X-Request-Id`):
`AuthenticationError` (401), `NotFoundError` (404), `ConflictError` (409,
for example `hostname_already_claimed`), `ValidationError` (422, for example
`apex_not_supported`), `RateLimitedError` (429, with `retryAfter` in
seconds) and `ServerError` (5xx). `TransportError` means no HTTP response
was received.

```ts
import { ConflictError, CustomDomainClient, ValidationError } from "@sireto/custom-domain";

declare const client: CustomDomainClient;

try {
  await client.createDomain({ hostname: "forms.customer.example", reference: "ws_8f3a1c" });
} catch (error) {
  if (error instanceof ConflictError && error.code === "hostname_already_claimed") {
    // another workspace already registered it
  } else if (error instanceof ValidationError) {
    console.log(error.code, error.details); // e.g. apex_not_supported
  } else {
    throw error;
  }
}
```

## Timeouts and retries

Each attempt has a 10 second timeout (`timeoutMs`). Requests that are safe
to repeat, that is `GET`, `DELETE`, the delivery replays, and `createDomain`
**when an idempotency key is given**, are retried up to `maxRetries`
(default 2) on connection errors, timeouts, 429 (waiting `Retry-After`) and
5xx, with exponential backoff from 0.5 seconds. A 429 asking for more than
`maxRetryDelayMs` (default 60 seconds) is thrown instead of waited for. A
create without an idempotency key, a recheck, a webhook create and a secret
rotation are never retried by the client: retrying a create could register
twice and a recheck is rate limited, so the caller decides. Redirects are
never followed, so the API key is only ever sent to `baseUrl`. Every request
carries `User-Agent: custom-domain-sdk-ts/<version>`.

`fetch` can be replaced, for example to add tracing or for tests:

```ts
import { CustomDomainClient } from "@sireto/custom-domain";

const client = new CustomDomainClient({
  baseUrl: "https://edge.example.net",
  apiKey: process.env.CUSTOM_DOMAIN_API_KEY!,
  timeoutMs: 5_000,
  maxRetries: 3,
  fetch: (input, init) => fetch(input, init),
});
```

## Verifying requests at your origin

The edge adds `X-Custom-Domain-Assertion` to every proxied request: a token
signed with a key the operator gives you, naming your application, the domain,
the customer's hostname and the **workspace reference** you registered.

**Select the workspace from the verified assertion's `reference` and from
nothing else.** Never from `Host`, a query parameter, a cookie or any other
header: those are under the visitor's control, the assertion is not
([edge-routing.md](https://github.com/sireto/custom-domain/blob/main/docs/edge-routing.md)).

All the helpers below take the same options:

| Option | Meaning |
|---|---|
| `keys` | Verification keys by key id, from the operator (`EDGE_ASSERTION_KEYS`, or your application's own `app_…` key). Give the current and the previous key during a rotation. |
| `applicationId` | Your application's id, from the operator. Always checked. |
| `workspaceLookup(reference)` | Returns something truthy only when you really serve that workspace; may be async. Used for the workspace probe below. |
| `onMissing` | `"reject"` (default): a request without an assertion gets 403. `"passthrough"`: it continues with no assertion, for an application that also serves its own domain. |
| `hostHeader` | `"customer"` (default) or `"origin"`, matching the origin's mode at the edge (see below). |
| `skew` | Allowed clock skew, in seconds (default 30). |

They reject an invalid assertion with `403` and a JSON body such as
`{"error": "assertion_expired", "message": "Assertion has expired"}`, exactly
as the Python middleware does, and they answer
`GET /.well-known/custom-domain-workspace`: they verify the assertion, call
your `workspaceLookup` with the reference, and return the reference only
when the lookup confirms the workspace exists (404 otherwise). The service's
lifecycle worker uses that answer to prove that routing and tenant selection
really work before it marks a domain ready, so `workspaceLookup` must consult
the same source your request handlers use. The operator's origin
verification probe (`GET /.well-known/custom-domain-origin-verification`)
comes from the API rather than through the edge and is let through without
an assertion.

### Express (and Connect, plain `node:http`)

```ts
import express from "express";
import { type Assertion, customDomainMiddleware } from "@sireto/custom-domain";

declare global {
  namespace Express {
    interface Request {
      customDomain?: Assertion | null;
    }
  }
}

const app = express();
app.use(
  customDomainMiddleware({
    keys: { "1": process.env.EDGE_ASSERTION_KEY! }, // current and previous key during rotation
    applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
    workspaceLookup: (reference) => loadWorkspace(reference), // returns the workspace or null
  }),
);

app.get("/", async (req, res) => {
  const workspace = await loadWorkspace(req.customDomain!.reference);
  res.json({ workspace: workspace?.name });
});
```

The verified assertion is on `req.customDomain` (`null` only with
`onMissing: "passthrough"`); `stateKey` names another property.

### Hono

```ts
import { Hono } from "hono";
import { type Assertion, createRequestVerifier } from "@sireto/custom-domain";

const verify = createRequestVerifier({
  keys: { "1": process.env.EDGE_ASSERTION_KEY! },
  applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
  workspaceLookup: loadWorkspace,
});

const app = new Hono<{ Variables: { customDomain: Assertion | null } }>();
app.use(async (c, next) => {
  const outcome = await verify(c.req.raw);
  if (outcome.response) return outcome.response; // 403, or the workspace probe's answer
  c.set("customDomain", outcome.assertion);
  await next();
});

app.get("/", async (c) => {
  const workspace = await loadWorkspace(c.get("customDomain")!.reference);
  return c.json({ workspace: workspace?.name });
});
```

### Next.js route handlers (App Router)

`withCustomDomain` wraps any Fetch API handler and passes the verified
assertion as its second argument:

```ts
// app/api/forms/route.ts
import { withCustomDomain } from "@sireto/custom-domain";

const options = {
  keys: { "1": process.env.EDGE_ASSERTION_KEY! },
  applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
  workspaceLookup: loadWorkspace,
};

export const GET = withCustomDomain(options, async (_request, assertion) => {
  const workspace = await loadWorkspace(assertion!.reference);
  return Response.json({ workspace: workspace?.name });
});
```

In Next.js middleware (`middleware.ts`), use `createRequestVerifier` to
refuse requests early and to answer the workspace probe. Middleware cannot
hand the assertion to a handler, so the handlers behind it still take the
workspace from a verified assertion (wrap them with `withCustomDomain`).

```ts
// middleware.ts
import { createRequestVerifier } from "@sireto/custom-domain";

const verify = createRequestVerifier({
  keys: { "1": process.env.EDGE_ASSERTION_KEY! },
  applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
  workspaceLookup: loadWorkspace,
});

export async function middleware(request: Request) {
  const outcome = await verify(request);
  if (outcome.response) return outcome.response;
  return undefined; // continue to the route
}
```

### Cloudflare Workers, Deno, Bun

```ts
import { withCustomDomain } from "@sireto/custom-domain";

interface Env {
  EDGE_ASSERTION_KEY: string;
  CUSTOM_DOMAIN_APPLICATION_ID: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const handler = withCustomDomain(
      {
        keys: { "1": env.EDGE_ASSERTION_KEY },
        applicationId: env.CUSTOM_DOMAIN_APPLICATION_ID,
        workspaceLookup: loadWorkspace,
      },
      async (_request, assertion) => new Response(`workspace ${assertion!.reference}`),
    );
    return handler(request);
  },
};
```

### Any framework

```ts
import { AssertionInvalid, WorkspaceResolver } from "@sireto/custom-domain";

declare const headers: Headers | Record<string, string | string[] | undefined>;
declare const host: string | undefined;

const resolver = new WorkspaceResolver({
  keys: { "1": process.env.EDGE_ASSERTION_KEY! },
  applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
});
try {
  const assertion = await resolver.resolve(headers, host);
  console.log(assertion.reference, assertion.domainId, assertion.payload.rid);
} catch (error) {
  if (!(error instanceof AssertionInvalid)) throw error;
  console.log(error.code); // missing, expired, bad_signature, wrong_application, ...
}
```

`verifyAssertion(token, keys, {applicationId, hostname})` is the bare check
underneath: it returns the `Assertion` (`reference`, `domainId`,
`applicationId`, `hostname`, `issuedAt`, `expiresAt`, `requestId`, `keyId`,
and the signed `payload` with `ref`, `dom`, `app`, `host`, `iat`, `exp`,
`rid`) or throws `AssertionInvalid` with one of the codes `missing`,
`malformed`, `unknown_key`, `bad_signature`, `not_yet_valid`, `expired`,
`wrong_application`, `wrong_hostname`.

Origins should only be reachable from the edge (network rules), or treat any
request without a valid assertion as a direct call and refuse it.

### Origin behind a CDN or a shared proxy

When the origin is registered in `origin` host-header mode, the edge sends
the origin's own name as `Host` and the customer's hostname as
`X-Forwarded-Host`. Pass the same mode, so the assertion is checked against
`X-Forwarded-Host`:

```ts
import { createRequestVerifier, WorkspaceResolver } from "@sireto/custom-domain";

const verify = createRequestVerifier({
  keys: { "1": process.env.EDGE_ASSERTION_KEY! },
  applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
  workspaceLookup: loadWorkspace,
  hostHeader: "origin",
});
const resolver = new WorkspaceResolver({
  keys: { "1": process.env.EDGE_ASSERTION_KEY! },
  applicationId: process.env.CUSTOM_DOMAIN_APPLICATION_ID!,
  hostHeader: "origin",
});
```

Proxies between the edge and your application must pass `X-Forwarded-Host`
through unchanged.

## Webhooks

```ts
import { CustomDomainClient } from "@sireto/custom-domain";

declare const client: CustomDomainClient;

const hook = await client.createWebhook({
  url: "https://app.acme.example/hooks/custom-domain",
  events: ["domain.ready", "domain.deleted"],
});
await storeSecret(hook.secret); // shown once
```

Verify every delivery against the **raw** body, before parsing it:

```ts
import express from "express";
import { parseEvent, SignatureInvalid, verifyWebhook } from "@sireto/custom-domain";

const app = express();
app.post("/hooks/custom-domain", express.raw({ type: "application/json" }), async (req, res) => {
  try {
    await verifyWebhook(req.get("X-Custom-Domain-Signature"), req.body as Buffer, [
      process.env.WEBHOOK_SECRET!, // the current secret
      process.env.WEBHOOK_SECRET_PREVIOUS ?? "", // and the previous one during a rotation
    ].filter(Boolean));
  } catch (error) {
    if (error instanceof SignatureInvalid) return res.status(400).end();
    throw error;
  }
  const event = parseEvent(req.body as Buffer); // event.id, event.type, event.created_at, event.data.domain
  console.log(event.type, event.data.domain.hostname);
  res.status(204).end();
});
```

With the Fetch API (Hono, Next.js, Workers), read the body once as text:

```ts
import { parseEvent, verifyWebhook } from "@sireto/custom-domain";

export async function POST(request: Request) {
  const body = await request.text();
  await verifyWebhook(request.headers.get("X-Custom-Domain-Signature"), body, [
    process.env.WEBHOOK_SECRET!,
  ]);
  const event = parseEvent(body);
  return Response.json({ received: event.id });
}
```

`verifyWebhook` throws `SignatureInvalid` with `missing`, `malformed`,
`stale` (more than `tolerance` seconds, 300 by default, from now) or
`bad_signature`, and returns the delivery timestamp. Deliveries are at least
once and may arrive out of order: deduplicate by `event.id` and compare
`event.created_at` before moving state ([webhooks.md](https://github.com/sireto/custom-domain/blob/main/docs/webhooks.md)).

## Development

```
npm ci
npm run typecheck         # the sources, the tests and every example in this README
npm test                  # unit tests, cross-language fixtures, generated-types check
npm run build             # dist/
npm run test:integration  # against the real service (needs uv; see below)
```

- **Types** come from the API contract: `npm run generate` rewrites
  `src/generated/openapi.ts` from `../docs/openapi.json`, and `npm test` fails
  when the committed file is out of date.
- **Cross-language fixtures** in `test/fixtures/` are tokens and signatures
  made by the service's signers, with the outcome the Python SDK gives for
  each. Regenerate them from the repository root after changing a case or
  the Python verifiers, and commit the result:

  ```
  uv run python sdk-ts/scripts/generate_fixtures.py
  ```

- **Integration test**: `npm run test:integration` starts the API on a
  temporary SQLite database, creates an application and a credential with
  the `custom-domain` CLI, and runs `test/integration.test.ts` against it.
  It needs the Python environment (`uv sync --all-packages` at the root).

## Compatibility

The SDK follows the API's `v1` contract and is released with the service
under the same version number. Minor releases add fields and methods; unknown
fields in API responses are passed through, so newer servers work with older
SDKs. Node 20 and newer are supported, as are runtimes with Web Crypto and
`fetch`.

## Licence

Apache-2.0.
