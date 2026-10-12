import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, describe, test } from "node:test";

import express from "express";
import { Hono } from "hono";

import {
  type Assertion,
  AssertionInvalid,
  createRequestVerifier,
  customDomainMiddleware,
  type CustomDomainRequest,
  ORIGIN_VERIFICATION_PATH,
  withCustomDomain,
  WORKSPACE_PATH,
  WorkspaceResolver,
} from "../src/index.js";
import { APP, HOST, KEYS, signAssertion } from "./helpers.js";

const WORKSPACES = new Set(["ws_8f3a1c"]);
const options = {
  keys: KEYS,
  applicationId: APP,
  workspaceLookup: async (reference: string) => WORKSPACES.has(reference),
};

function request(path: string, headers: Record<string, string> = {}, method = "GET") {
  return new Request(`https://${HOST}${path}`, { method, headers });
}

describe("WorkspaceResolver", () => {
  test("validates its options", () => {
    assert.throws(
      () => new WorkspaceResolver({ keys: KEYS, applicationId: APP, hostHeader: "sni" as never }),
      TypeError,
    );
    assert.throws(() => new WorkspaceResolver({ keys: KEYS, applicationId: "" }), TypeError);
  });

  test("reads Headers, plain objects with arrays, and entry lists", async () => {
    const token = await signAssertion();
    const resolver = new WorkspaceResolver({ keys: KEYS, applicationId: APP });
    for (const headers of [
      new Headers({ "X-Custom-Domain-Assertion": token }),
      { "x-custom-domain-assertion": token, other: ["a", "b"] },
      [["X-Custom-Domain-Assertion", token]] as [string, string][],
    ]) {
      assert.equal((await resolver.resolve(headers, HOST)).reference, "ws_8f3a1c");
    }
  });

  test("copies the keys, so later changes to the caller's object do not apply", async () => {
    const keys: Record<string, string> = { ...KEYS };
    const resolver = new WorkspaceResolver({ keys, applicationId: APP });
    delete keys["1"];
    const token = await signAssertion();
    assert.equal((await resolver.resolve({ "X-Custom-Domain-Assertion": token }, HOST)).keyId, "1");
  });
});

describe("createRequestVerifier (Fetch API)", () => {
  const verify = createRequestVerifier(options);

  test("passes a valid assertion through", async () => {
    const outcome = await verify(request("/", { "X-Custom-Domain-Assertion": await signAssertion() }));
    assert.equal(outcome.response, undefined);
    assert.equal(outcome.assertion?.reference, "ws_8f3a1c");
  });

  test("uses the Host header when it differs from the URL", async () => {
    // Behind a proxy the URL can name the origin while Host is the customer's.
    const req = new Request("http://127.0.0.1:3000/", {
      headers: { host: HOST, "X-Custom-Domain-Assertion": await signAssertion() },
    });
    assert.equal((await verify(req)).assertion?.hostname, HOST);
    // Without a Host header the URL's host is used.
    const byUrl = new Request(`https://${HOST}/`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion() },
    });
    assert.equal((await verify(byUrl)).assertion?.hostname, HOST);
  });

  test("rejects an invalid assertion with 403 and the Python body", async () => {
    const outcome = await verify(
      request("/", { "X-Custom-Domain-Assertion": await signAssertion({ host: "other.example" }) }),
    );
    assert.equal(outcome.response?.status, 403);
    assert.equal(outcome.response?.headers.get("content-type"), "application/json");
    assert.deepEqual(await outcome.response?.json(), {
      error: "assertion_wrong_hostname",
      message: "Assertion is for another hostname",
    });
  });

  test("rejects a missing assertion by default and passes it in passthrough mode", async () => {
    const rejected = await verify(request("/"));
    assert.equal(rejected.response?.status, 403);
    assert.deepEqual(await rejected.response?.json(), {
      error: "assertion_missing",
      message: "No assertion header",
    });
    const passthrough = createRequestVerifier({ ...options, onMissing: "passthrough" });
    const passed = await passthrough(request("/"));
    assert.equal(passed.response, undefined);
    assert.equal(passed.assertion, null);
    // Passthrough never lets an invalid assertion through.
    const bad = await passthrough(request("/", { "X-Custom-Domain-Assertion": "garbage" }));
    assert.equal(bad.response?.status, 403);
  });

  test("lets the origin-verification probe through without an assertion (GET only)", async () => {
    const outcome = await verify(request(ORIGIN_VERIFICATION_PATH));
    assert.equal(outcome.response, undefined);
    assert.equal(outcome.assertion, null);
    const post = await verify(request(ORIGIN_VERIFICATION_PATH, {}, "POST"));
    assert.equal(post.response?.status, 403);
  });

  test("answers the workspace probe", async () => {
    const ok = await verify(
      request(WORKSPACE_PATH, { "X-Custom-Domain-Assertion": await signAssertion() }),
    );
    assert.equal(ok.response?.status, 200);
    assert.deepEqual(await ok.response?.json(), { reference: "ws_8f3a1c", application: APP });

    const unknown = await verify(
      request(WORKSPACE_PATH, { "X-Custom-Domain-Assertion": await signAssertion({ ref: "ws_gone" }) }),
    );
    assert.equal(unknown.response?.status, 404);
    assert.deepEqual(await unknown.response?.json(), { error: "workspace_not_found" });

    const passthrough = createRequestVerifier({ ...options, onMissing: "passthrough" });
    const missing = await passthrough(request(WORKSPACE_PATH));
    assert.equal(missing.response?.status, 403);
    assert.deepEqual(await missing.response?.json(), { error: "assertion_missing" });
  });

  test("a synchronous workspaceLookup works too", async () => {
    const verifySync = createRequestVerifier({ ...options, workspaceLookup: (r) => r === "ws_8f3a1c" });
    const ok = await verifySync(
      request(WORKSPACE_PATH, { "X-Custom-Domain-Assertion": await signAssertion() }),
    );
    assert.equal(ok.response?.status, 200);
  });

  test("checks X-Forwarded-Host in origin mode", async () => {
    const verifyOrigin = createRequestVerifier({ ...options, hostHeader: "origin" });
    const token = await signAssertion();
    const req = new Request("https://app.saas.example/", {
      headers: { "X-Custom-Domain-Assertion": token, "X-Forwarded-Host": HOST },
    });
    assert.equal((await verifyOrigin(req)).assertion?.reference, "ws_8f3a1c");
    const missing = new Request("https://app.saas.example/", {
      headers: { "X-Custom-Domain-Assertion": token },
    });
    assert.equal((await verifyOrigin(missing)).response?.status, 403);
  });

  test("validates its options", () => {
    assert.throws(() => createRequestVerifier({ ...options, onMissing: "allow" as never }), TypeError);
    assert.throws(
      () => createRequestVerifier({ ...options, workspaceLookup: undefined as never }),
      TypeError,
    );
  });
});

describe("withCustomDomain", () => {
  test("calls the handler with the assertion and extra arguments", async () => {
    const handler = withCustomDomain(options, (_request, assertion, env: { name: string }) =>
      Response.json({ workspace: assertion?.reference, env: env.name }),
    );
    const response = await handler(
      request("/", { "X-Custom-Domain-Assertion": await signAssertion() }),
      { name: "prod" },
    );
    assert.deepEqual(await response.json(), { workspace: "ws_8f3a1c", env: "prod" });
    const refused = await handler(request("/"), { name: "prod" });
    assert.equal(refused.status, 403);
  });
});

describe("Hono", () => {
  test("a middleware built on createRequestVerifier", async () => {
    const verify = createRequestVerifier(options);
    const app = new Hono<{ Variables: { customDomain: Assertion | null } }>();
    app.use(async (c, next) => {
      const outcome = await verify(c.req.raw);
      if (outcome.response) return outcome.response;
      c.set("customDomain", outcome.assertion);
      await next();
    });
    app.get("/", (c) => c.json({ workspace: c.get("customDomain")?.reference }));
    const ok = await app.request(`https://${HOST}/`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion() },
    });
    assert.deepEqual(await ok.json(), { workspace: "ws_8f3a1c" });
    const refused = await app.request(`https://${HOST}/`);
    assert.equal(refused.status, 403);
    const probe = await app.request(`https://${HOST}${WORKSPACE_PATH}`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion() },
    });
    assert.deepEqual(await probe.json(), { reference: "ws_8f3a1c", application: APP });
  });
});

async function listen(server: Server): Promise<string> {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

describe("Express and node:http", () => {
  let expressServer: Server;
  let expressUrl: string;
  let plainServer: Server;
  let plainUrl: string;
  const thrown = new Error("lookup failed");

  before(async () => {
    const app = express();
    app.use(customDomainMiddleware(options));
    app.get("/", (req, res) => {
      res.json({ workspace: (req as CustomDomainRequest).customDomain?.reference ?? null });
    });
    app.get(ORIGIN_VERIFICATION_PATH, (_req, res) => {
      res.type("text/plain").send("token");
    });
    expressServer = createServer(app);
    expressUrl = await listen(expressServer);

    const middleware = customDomainMiddleware({ ...options, onMissing: "passthrough", stateKey: "cd" });
    plainServer = createServer((req, res) => {
      middleware(req, res, (error) => {
        res.setHeader("Content-Type", "application/json");
        res.end(
          JSON.stringify({
            error: error ? String(error) : null,
            workspace: (req as unknown as { cd: Assertion | null }).cd?.reference ?? null,
          }),
        );
      });
    });
    plainUrl = await listen(plainServer);
  });

  after(() => {
    expressServer.close();
    plainServer.close();
  });

  test("attaches req.customDomain", async () => {
    const response = await fetch(`${expressUrl}/?q=1`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion({ host: "127.0.0.1" }) },
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { workspace: "ws_8f3a1c" });
  });

  test("rejects with 403 JSON", async () => {
    const response = await fetch(`${expressUrl}/`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion() },
    });
    assert.equal(response.status, 403);
    assert.equal(response.headers.get("content-type"), "application/json");
    assert.deepEqual(await response.json(), {
      error: "assertion_wrong_hostname",
      message: "Assertion is for another hostname",
    });
    const missing = await fetch(`${expressUrl}/`);
    assert.equal(missing.status, 403);
  });

  test("answers the workspace probe and lets origin verification through", async () => {
    const probe = await fetch(`${expressUrl}${WORKSPACE_PATH}`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion({ host: "127.0.0.1" }) },
    });
    assert.deepEqual(await probe.json(), { reference: "ws_8f3a1c", application: APP });
    const verification = await fetch(`${expressUrl}${ORIGIN_VERIFICATION_PATH}`);
    assert.equal(await verification.text(), "token");
  });

  test("plain node:http with passthrough and a custom state key", async () => {
    const anonymous = await fetch(`${plainUrl}/`);
    assert.deepEqual(await anonymous.json(), { error: null, workspace: null });
    const verified = await fetch(`${plainUrl}/`, {
      headers: { "X-Custom-Domain-Assertion": await signAssertion({ host: "127.0.0.1" }) },
    });
    assert.deepEqual(await verified.json(), { error: null, workspace: "ws_8f3a1c" });
    const bad = await fetch(`${plainUrl}/`, { headers: { "X-Custom-Domain-Assertion": "v1.x.y.z" } });
    assert.equal(bad.status, 403);
  });

  test("a lookup that throws reaches next(error)", async () => {
    const middleware = customDomainMiddleware({
      ...options,
      workspaceLookup: () => {
        throw thrown;
      },
    });
    const token = await signAssertion();
    const error = await new Promise((resolve) => {
      middleware(
        { method: "GET", url: WORKSPACE_PATH, headers: { host: HOST, "x-custom-domain-assertion": token } },
        { statusCode: 200, setHeader: () => {}, end: () => resolve("responded") },
        resolve,
      );
    });
    assert.equal(error, thrown);
  });
});

test("AssertionInvalid carries a stable code", () => {
  const error = new AssertionInvalid("expired", "Assertion has expired");
  assert.equal(error.code, "expired");
  assert.equal(error.name, "AssertionInvalid");
  assert.ok(error instanceof Error);
});
