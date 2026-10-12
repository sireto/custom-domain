// Cross-language parity: the fixtures are tokens and signatures made by the
// service's signers, with the outcome the Python SDK gives for each case
// (scripts/generate_fixtures.py). The TypeScript verifiers must agree on
// every case, accepted or refused, and on the refusal code.

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { type Assertion, AssertionInvalid, verifyAssertion, WorkspaceResolver } from "../src/index.js";
import { SignatureInvalid, verifyWebhook } from "../src/index.js";
import { readJson } from "./helpers.js";

type Expect<T> = { ok: true; assertion?: T; timestamp?: number } | { ok: false; code: string };

interface AssertionCase {
  name: string;
  kind: "verify" | "resolve";
  keys: Record<string, string>;
  applicationId: string;
  now: number;
  skew: number;
  token?: string | null;
  hostname?: string | null;
  hostHeader?: "customer" | "origin";
  headers?: Record<string, string>;
  host?: string | null;
  expect: Expect<Omit<Assertion, "payload">>;
}

interface WebhookCase {
  name: string;
  header: string | null;
  body: string;
  secrets: string[];
  now: number;
  tolerance: number;
  expect: Expect<never>;
}

async function outcome<T>(call: () => Promise<T>, errorType: new (...args: never[]) => Error) {
  try {
    return { ok: true as const, value: await call() };
  } catch (error) {
    if (!(error instanceof errorType)) throw error;
    return { ok: false as const, code: (error as Error & { code: string }).code };
  }
}

const assertions = readJson<{ cases: AssertionCase[] }>("test/fixtures/assertions.json").cases;
const webhooks = readJson<{ cases: WebhookCase[] }>("test/fixtures/webhooks.json").cases;

describe("assertion fixtures from the Python SDK", () => {
  test("cover every refusal code", () => {
    const codes = new Set(assertions.map((c) => (c.expect.ok ? "ok" : c.expect.code)));
    for (const code of [
      "ok",
      "missing",
      "malformed",
      "unknown_key",
      "bad_signature",
      "not_yet_valid",
      "expired",
      "wrong_application",
      "wrong_hostname",
    ]) {
      assert.ok(codes.has(code), code);
    }
  });

  for (const c of assertions) {
    test(c.name, async () => {
      const result = await outcome(() => {
        if (c.kind === "verify") {
          return verifyAssertion(c.token, c.keys, {
            applicationId: c.applicationId,
            hostname: c.hostname ?? null,
            now: c.now,
            skew: c.skew,
          });
        }
        const resolver = new WorkspaceResolver({
          keys: c.keys,
          applicationId: c.applicationId,
          skew: c.skew,
          hostHeader: c.hostHeader ?? "customer",
        });
        return resolver.resolve(c.headers ?? {}, c.host, { now: c.now });
      }, AssertionInvalid);
      if (!c.expect.ok) {
        assert.deepEqual(result, { ok: false, code: c.expect.code });
        return;
      }
      assert.equal(result.ok, true, `expected success, got ${JSON.stringify(result)}`);
      if (!result.ok) return;
      const { payload, ...fields } = result.value;
      assert.deepEqual(fields, c.expect.assertion);
      assert.equal(payload.ref, fields.reference);
      assert.equal(payload.app, fields.applicationId);
      assert.equal(payload.host, fields.hostname);
    });
  }

  test("a Map of keys works like an object", async () => {
    const c = assertions.find((x) => x.name === "valid")!;
    const result = await verifyAssertion(c.token, new Map(Object.entries(c.keys)), {
      applicationId: c.applicationId,
      hostname: c.hostname ?? null,
      now: c.now,
    });
    assert.equal(result.reference, "ws_8f3a1c");
  });

  test("a key given as bytes works like a string", async () => {
    const c = assertions.find((x) => x.name === "valid")!;
    const keys = Object.fromEntries(
      Object.entries(c.keys).map(([id, secret]) => [id, new TextEncoder().encode(secret)]),
    );
    const result = await verifyAssertion(c.token, keys, {
      applicationId: c.applicationId,
      now: c.now,
    });
    assert.equal(result.keyId, "1");
  });
});

describe("webhook fixtures from the Python SDK", () => {
  test("cover every outcome", () => {
    const codes = new Set(webhooks.map((c) => (c.expect.ok ? "ok" : c.expect.code)));
    for (const code of ["ok", "missing", "malformed", "stale", "bad_signature"]) {
      assert.ok(codes.has(code), code);
    }
  });

  for (const c of webhooks) {
    test(c.name, async () => {
      for (const body of [c.body, new TextEncoder().encode(c.body)]) {
        const result = await outcome(
          () => verifyWebhook(c.header, body, c.secrets, { now: c.now, tolerance: c.tolerance }),
          SignatureInvalid,
        );
        if (c.expect.ok) {
          assert.deepEqual(result, { ok: true, value: c.expect.timestamp });
        } else {
          assert.deepEqual(result, { ok: false, code: c.expect.code });
        }
      }
    });
  }
});
