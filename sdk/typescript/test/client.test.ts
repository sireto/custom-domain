import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  ApiError,
  AuthenticationError,
  ConflictError,
  CredentialReadOnlyError,
  CustomDomainClient,
  findCheck,
  isReady,
  NotFoundError,
  PermissionDeniedError,
  RateLimitedError,
  renderDnsInstructions,
  ServerError,
  TransportError,
  USER_AGENT,
  ValidationError,
  VERSION,
} from "../src/index.js";
import { domainJson } from "./helpers.js";

interface Seen {
  method: string;
  url: URL;
  headers: Headers;
  body: unknown;
}

type Reply = Response | Error | ((seen: Seen, init: RequestInit) => Response | Promise<Response>);

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function apiError(status: number, code: string, details: Record<string, unknown> = {}) {
  return json(status, { error: { code, message: `${code} happened`, details } });
}

function fake(...replies: Reply[]) {
  const seen: Seen[] = [];
  const sleeps: number[] = [];
  const fetch = async (input: string, init: RequestInit) => {
    const entry: Seen = {
      method: init.method ?? "GET",
      url: new URL(input),
      headers: new Headers(init.headers),
      body: init.body === undefined ? undefined : JSON.parse(String(init.body)),
    };
    seen.push(entry);
    const reply = replies.shift();
    if (reply === undefined) throw new Error("unexpected request");
    if (reply instanceof Error) throw reply;
    return typeof reply === "function" ? reply(entry, init) : reply;
  };
  const client = new CustomDomainClient({
    baseUrl: "https://edge.example.net/",
    apiKey: "cd_test_secret",
    fetch,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
  });
  return { client, seen, sleeps };
}

describe("CustomDomainClient", () => {
  test("refuses anything but an application credential", () => {
    assert.throws(
      () => new CustomDomainClient({ baseUrl: "https://x", apiKey: "not-a-credential" }),
      TypeError,
    );
  });

  test("creates a domain with headers, body and idempotency key", async () => {
    const { client, seen } = fake(json(201, domainJson({ metadata: { plan: "pro" } })));
    const domain = await client.createDomain(
      { hostname: "Forms.Customer.Example", reference: "ws_8f3a1c", metadata: { plan: "pro" } },
      { idempotencyKey: "signup-ws_8f3a1c" },
    );
    assert.equal(domain.status, "pending_dns");
    const [request] = seen;
    assert.equal(request!.method, "POST");
    assert.equal(request!.url.href, "https://edge.example.net/v1/domains");
    assert.equal(request!.headers.get("authorization"), "Bearer cd_test_secret");
    assert.equal(request!.headers.get("user-agent"), `custom-domain-sdk-ts/${VERSION}`);
    assert.equal(USER_AGENT, `custom-domain-sdk-ts/${VERSION}`);
    assert.equal(request!.headers.get("idempotency-key"), "signup-ws_8f3a1c");
    assert.equal(request!.headers.get("content-type"), "application/json");
    assert.deepEqual(request!.body, {
      hostname: "Forms.Customer.Example",
      reference: "ws_8f3a1c",
      metadata: { plan: "pro" },
    });
  });

  test("accepts a base URL that already ends in /v1", async () => {
    const { seen } = fake();
    const client = new CustomDomainClient({
      baseUrl: "https://edge.example.net/v1/",
      apiKey: "cd_x",
      fetch: async (input) => {
        seen.push({ method: "GET", url: new URL(input), headers: new Headers(), body: undefined });
        return json(200, []);
      },
    });
    await client.listWebhooks();
    assert.equal(seen[0]!.url.pathname, "/v1/webhooks");
  });

  test("domain helpers", () => {
    const domain = domainJson() as Parameters<typeof isReady>[0];
    assert.equal(isReady(domain), false);
    assert.equal(findCheck(domain, "ownership")?.status, "pending");
    const text = renderDnsInstructions(domain);
    assert.match(text, /^Create these DNS records for forms\.customer\.example:/);
    assert.match(text, /TXT   _custom-domain-challenge\.forms\.customer\.example {2}-> {2}custom-domain-verify=abc/);
    assert.match(text, /purpose: routing\. Point the hostname/);
  });

  test("lists with filters and iterates every page", async () => {
    const { client, seen } = fake(
      json(200, { items: [domainJson({ id: "a" })], limit: 1, offset: 0, next_offset: 1 }),
      json(200, { items: [domainJson({ id: "b" })], limit: 1, offset: 1, next_offset: null }),
    );
    const ids: string[] = [];
    for await (const domain of client.iterDomains({ reference: "ws_8f3a1c", pageSize: 1 })) {
      ids.push(domain.id);
    }
    assert.deepEqual(ids, ["a", "b"]);
    assert.equal(seen[0]!.url.search, "?limit=1&offset=0&reference=ws_8f3a1c");
    assert.equal(seen[1]!.url.searchParams.get("offset"), "1");

    const page = fake(json(200, { items: [], limit: 50, offset: 0, next_offset: null }));
    await page.client.listDomains({ status: "ready", includeDeleted: true });
    assert.equal(page.seen[0]!.url.search, "?limit=50&offset=0&status=ready&include_deleted=true");
  });

  test("gets, rechecks and deletes by id, encoding the path segment", async () => {
    const { client, seen } = fake(
      json(200, domainJson()),
      json(202, domainJson()),
      json(202, domainJson({ status: "deleting", dns_records: [] })),
      json(200, domainJson()),
    );
    await client.getDomain("abc");
    await client.recheckDomain("abc");
    const deleted = await client.deleteDomain("abc");
    assert.equal(deleted.status, "deleting");
    await client.getDomain("../webhooks?x=1", { includeDeleted: true });
    assert.deepEqual(
      seen.map((s) => `${s.method} ${s.url.pathname}${s.url.search}`),
      [
        "GET /v1/domains/abc",
        "POST /v1/domains/abc/checks",
        "DELETE /v1/domains/abc",
        "GET /v1/domains/..%2Fwebhooks%3Fx%3D1?include_deleted=true",
      ],
    );
  });

  test("webhook routes", async () => {
    const hook = {
      id: "h1",
      url: "https://app.example/hooks",
      events: ["domain.ready"],
      active: true,
      created_at: "2026-09-25T14:00:00Z",
      revoked_at: null,
      previous_secret_expires_at: null,
    };
    const delivery = {
      id: "d1",
      event_id: "e1",
      event_type: "domain.ready",
      domain_id: null,
      state: "pending",
      attempts: 0,
      next_attempt_at: null,
      delivered_at: null,
      abandoned_at: null,
      last_attempt_at: null,
      last_status: null,
      last_error: null,
      created_at: "2026-09-25T14:00:00Z",
    };
    const { client, seen } = fake(
      json(201, { ...hook, secret: "whsec_1" }),
      json(200, [hook]),
      json(200, { ...hook, secret: "whsec_2" }),
      json(200, [delivery]),
      json(202, delivery),
      json(202, { requeued: 3 }),
      json(200, { ...hook, active: false }),
    );
    const created = await client.createWebhook({
      url: "https://app.example/hooks",
      events: ["domain.ready"],
    });
    assert.equal(created.secret, "whsec_1");
    assert.equal((await client.listWebhooks()).length, 1);
    assert.equal((await client.rotateWebhook("h1")).secret, "whsec_2");
    assert.equal((await client.listDeliveries("h1", { state: "pending" }))[0]!.id, "d1");
    assert.equal((await client.replayDelivery("h1", "d1")).state, "pending");
    assert.equal(await client.replaySince("h1", new Date("2026-01-01T00:00:00Z")), 3);
    assert.equal((await client.deleteWebhook("h1")).active, false);
    assert.deepEqual(
      seen.map((s) => `${s.method} ${s.url.pathname}${s.url.search}`),
      [
        "POST /v1/webhooks",
        "GET /v1/webhooks",
        "POST /v1/webhooks/h1/rotate",
        "GET /v1/webhooks/h1/deliveries?limit=50&offset=0&state=pending",
        "POST /v1/webhooks/h1/deliveries/d1/replay",
        "POST /v1/webhooks/h1/replay?since=2026-01-01T00%3A00%3A00.000Z",
        "DELETE /v1/webhooks/h1",
      ],
    );
    assert.deepEqual(seen[0]!.body, { url: "https://app.example/hooks", events: ["domain.ready"] });
  });

  test("maps each status to its error class with code, message, details and request id", async () => {
    const cases: [number, string, new (...a: never[]) => ApiError][] = [
      [401, "unauthorized", AuthenticationError],
      [404, "domain_not_found", NotFoundError],
      [409, "hostname_already_claimed", ConflictError],
      [422, "apex_not_supported", ValidationError],
      [403, "application_suspended", PermissionDeniedError],
      [403, "credential_read_only", CredentialReadOnlyError],
      [400, "bad_request", ApiError],
    ];
    for (const [status, code, type] of cases) {
      const { client } = fake(
        new Response(JSON.stringify({ error: { code, message: "no", details: { field: "hostname" } } }), {
          status,
          headers: { "X-Request-Id": "rid-7" },
        }),
      );
      await assert.rejects(client.createDomain({ hostname: "x.example", reference: "r" }), (error) => {
        assert.ok(error instanceof type, `${status} -> ${(error as Error).name}`);
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, status);
        assert.equal(error.code, code);
        assert.equal(error.message, "no");
        assert.deepEqual(error.details, { field: "hostname" });
        assert.equal(error.requestId, "rid-7");
        assert.equal(error.name, type.name);
        return true;
      });
    }
  });

  test("a body that is not the error envelope still gives an ApiError", async () => {
    const { client } = fake(new Response("<html>Bad gateway</html>", { status: 418 }));
    await assert.rejects(client.getDomain("a"), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.code, "http_error");
      assert.equal(error.message, "<html>Bad gateway</html>");
      assert.deepEqual(error.details, {});
      return true;
    });
  });

  test("retries idempotent requests on 5xx and transport errors with backoff", async () => {
    const { client, seen, sleeps } = fake(
      apiError(503, "unavailable"),
      new TypeError("fetch failed"),
      json(200, domainJson()),
    );
    await client.getDomain("a");
    assert.equal(seen.length, 3);
    assert.deepEqual(sleeps, [500, 1000]);
  });

  test("gives up after maxRetries with the last error", async () => {
    const { client, seen } = fake(
      apiError(500, "boom"),
      apiError(502, "bad_gateway"),
      apiError(503, "unavailable"),
    );
    await assert.rejects(client.deleteDomain("a"), (error) => {
      assert.ok(error instanceof ServerError);
      assert.equal(error.code, "unavailable");
      return true;
    });
    assert.equal(seen.length, 3);

    const transport = fake(new TypeError("a"), new TypeError("b"), new TypeError("c"));
    await assert.rejects(transport.client.listWebhooks(), TransportError);
  });

  test("honours Retry-After on 429", async () => {
    const withHeader = fake(
      new Response(JSON.stringify({ error: { code: "rate_limited", message: "slow down" } }), {
        status: 429,
        headers: { "Retry-After": "7" },
      }),
      json(200, domainJson()),
    );
    await withHeader.client.getDomain("a");
    assert.deepEqual(withHeader.sleeps, [7000]);
    // Without the header the wait is one second, as in the Python SDK.
    const without = fake(apiError(429, "rate_limited"), json(200, domainJson()));
    await without.client.getDomain("a");
    assert.deepEqual(without.sleeps, [1000]);
  });

  test("throws a 429 that asks for longer than maxRetryDelayMs", async () => {
    const { client, sleeps } = fake(
      new Response(JSON.stringify({ error: { code: "rate_limited", message: "later" } }), {
        status: 429,
        headers: { "Retry-After": "3600" },
      }),
    );
    await assert.rejects(client.getDomain("a"), (error) => {
      assert.ok(error instanceof RateLimitedError);
      assert.equal(error.retryAfter, 3600);
      return true;
    });
    assert.deepEqual(sleeps, []);
  });

  test("never retries a create without an idempotency key, a recheck, a webhook create or a rotation", async () => {
    for (const call of [
      (c: CustomDomainClient) => c.createDomain({ hostname: "x.example", reference: "r" }),
      (c: CustomDomainClient) => c.recheckDomain("a"),
      (c: CustomDomainClient) => c.createWebhook({ url: "https://x", events: ["domain.ready"] }),
      (c: CustomDomainClient) => c.rotateWebhook("h"),
    ]) {
      const { client, seen } = fake(apiError(503, "unavailable"), json(200, {}));
      await assert.rejects(call(client), ServerError);
      assert.equal(seen.length, 1);
    }
    const limited = fake(apiError(429, "rate_limited"), json(202, domainJson()));
    await assert.rejects(limited.client.recheckDomain("a"), RateLimitedError);
    assert.equal(limited.seen.length, 1);
  });

  test("retries a create that has an idempotency key", async () => {
    const { client, seen } = fake(apiError(502, "bad_gateway"), json(200, domainJson()));
    await client.createDomain({ hostname: "x.example", reference: "r" }, { idempotencyKey: "k" });
    assert.equal(seen.length, 2);
    assert.equal(seen[1]!.headers.get("idempotency-key"), "k");
  });

  test("does not retry other 4xx", async () => {
    const { client, seen } = fake(apiError(404, "domain_not_found"), json(200, domainJson()));
    await assert.rejects(client.getDomain("a"), NotFoundError);
    assert.equal(seen.length, 1);
  });

  test("times out a request that never answers", async () => {
    const hang = (_seen: Seen, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal!.addEventListener("abort", () => reject(init.signal!.reason));
      });
    const client = new CustomDomainClient({
      baseUrl: "https://edge.example.net",
      apiKey: "cd_x",
      timeoutMs: 20,
      maxRetries: 1,
      sleep: async () => {},
      fetch: (_input, init) => hang({} as Seen, init),
    });
    await assert.rejects(client.getDomain("a"), (error) => {
      assert.ok(error instanceof TransportError);
      assert.match(error.message, /no response within 20 ms/);
      return true;
    });
  });

  test("a caller's abort stops the request and its retries", async () => {
    const controller = new AbortController();
    let calls = 0;
    const client = new CustomDomainClient({
      baseUrl: "https://edge.example.net",
      apiKey: "cd_x",
      sleep: async () => {},
      fetch: (_input, init) => {
        calls += 1;
        return new Promise<Response>((_resolve, reject) => {
          init.signal!.addEventListener("abort", () => reject(init.signal!.reason));
          controller.abort(new Error("caller gave up"));
        });
      },
    });
    await assert.rejects(client.getDomain("a", { signal: controller.signal }), /caller gave up/);
    assert.equal(calls, 1);
  });

  test("never follows a redirect", async () => {
    const { client, seen } = fake((_seen, init) => {
      assert.equal(init.redirect, "manual");
      return new Response(null, { status: 302, headers: { Location: "https://elsewhere.example/" } });
    });
    await assert.rejects(client.listWebhooks(), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 302);
      return true;
    });
    assert.equal(seen.length, 1);
  });
});

test("a read-only key's refusal is a CredentialReadOnlyError, which is a PermissionDeniedError and isn't retried", async () => {
  let calls = 0;
  const client = new CustomDomainClient({
    baseUrl: "https://edge.example.net",
    apiKey: "cd_test_key",
    fetch: async () => {
      calls += 1;
      return new Response(
        JSON.stringify({
          error: { code: "credential_read_only", message: "This API key is read-only.", details: { scope: "read", method: "DELETE" } },
        }),
        { status: 403 },
      );
    },
  });
  await assert.rejects(client.deleteDomain("d"), (error) => {
    assert.ok(error instanceof CredentialReadOnlyError);
    assert.ok(error instanceof PermissionDeniedError);
    assert.deepEqual((error as CredentialReadOnlyError).details, { scope: "read", method: "DELETE" });
    return true;
  });
  assert.equal(calls, 1);
});
