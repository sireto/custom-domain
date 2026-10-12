// The client against the real service. scripts/integration-test.sh starts the
// API on SQLite, creates an application and a credential with the CLI, and
// runs this file with CUSTOM_DOMAIN_TEST_URL and CUSTOM_DOMAIN_TEST_API_KEY
// set. Without them (a plain `npm test`) it is skipped.

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  AuthenticationError,
  ConflictError,
  CustomDomainClient,
  findCheck,
  isReady,
  NotFoundError,
  renderDnsInstructions,
  ValidationError,
} from "../src/index.js";

const baseUrl = process.env.CUSTOM_DOMAIN_TEST_URL;
const apiKey = process.env.CUSTOM_DOMAIN_TEST_API_KEY;
const skip = !baseUrl || !apiKey ? "set CUSTOM_DOMAIN_TEST_URL and CUSTOM_DOMAIN_TEST_API_KEY" : false;

describe("against the real API", { skip }, () => {
  const client = new CustomDomainClient({ baseUrl: baseUrl ?? "", apiKey: apiKey ?? "cd_" });

  test("domain lifecycle", async () => {
    const domain = await client.createDomain(
      { hostname: "Forms.Customer.Example", reference: "ws_1", metadata: { plan: "pro" } },
      { idempotencyKey: "ts-k1" },
    );
    assert.equal(domain.hostname, "forms.customer.example");
    assert.equal(domain.status, "pending_dns");
    assert.ok(!isReady(domain));
    assert.deepEqual(domain.metadata, { plan: "pro" });
    const [txt, cname] = domain.dns_records;
    assert.equal(txt?.type, "TXT");
    assert.equal(txt?.purpose, "ownership");
    assert.ok(txt?.value.startsWith("custom-domain-verify="));
    assert.equal(cname?.type, "CNAME");
    assert.equal(cname?.value, "acme.edge.example.net");
    assert.deepEqual(
      domain.checks.map((c) => c.type),
      ["ownership", "routing", "certificate", "origin"],
    );
    assert.equal(findCheck(domain, "ownership")?.status, "pending");
    assert.match(renderDnsInstructions(domain), /_custom-domain-challenge\.forms\.customer\.example/);

    const replay = await client.createDomain(
      { hostname: "Forms.Customer.Example", reference: "ws_1", metadata: { plan: "pro" } },
      { idempotencyKey: "ts-k1" },
    );
    assert.equal(replay.id, domain.id);

    await assert.rejects(
      client.createDomain({ hostname: "forms.customer.example", reference: "ws_2" }),
      (error) => error instanceof ConflictError && error.code === "hostname_already_claimed" && error.status === 409,
    );
    await assert.rejects(
      client.createDomain({ hostname: "customer.example", reference: "ws_2" }),
      (error) => error instanceof ValidationError && error.code === "apex_not_supported",
    );
    await assert.rejects(
      client.createDomain({ hostname: "forms.customer.example", reference: "ws_other" }, { idempotencyKey: "ts-k1" }),
      (error) => error instanceof ValidationError && error.code === "idempotency_key_reused",
    );

    const fetched = await client.getDomain(domain.id);
    assert.equal(fetched.id, domain.id);
    assert.ok(!Number.isNaN(Date.parse(fetched.created_at)));

    const second = await client.createDomain({ hostname: "shop.customer.example", reference: "ws_1" });
    const page = await client.listDomains({ reference: "ws_1", limit: 1 });
    assert.equal(page.items.length, 1);
    assert.equal(page.next_offset, 1);
    const all: string[] = [];
    for await (const d of client.iterDomains({ reference: "ws_1", pageSize: 1 })) all.push(d.id);
    assert.deepEqual(all, [domain.id, second.id]);
    assert.equal((await client.listDomains({ status: "ready" })).items.length, 0);

    const rechecked = await client.recheckDomain(domain.id);
    assert.equal(rechecked.id, domain.id);

    const deleted = await client.deleteDomain(domain.id);
    assert.equal(deleted.status, "deleting");
    assert.deepEqual(deleted.dns_records, []);
    assert.ok(deleted.deleted_at);
    assert.equal((await client.deleteDomain(domain.id)).status, "deleting");
    await assert.rejects(client.getDomain(domain.id), (error) => error instanceof NotFoundError && error.code === "domain_not_found");
    assert.equal((await client.getDomain(domain.id, { includeDeleted: true })).status, "deleting");
    const remaining = await client.listDomains({ reference: "ws_1" });
    assert.deepEqual(remaining.items.map((d) => d.id), [second.id]);
    const withDeleted = await client.listDomains({ reference: "ws_1", includeDeleted: true });
    assert.equal(withDeleted.items.length, 2);
    await client.deleteDomain(second.id);
  });

  test("webhooks", async () => {
    const hook = await client.createWebhook({
      url: "https://localhost/hooks",
      events: ["domain.ready", "domain.deleted"],
    });
    assert.ok(hook.secret.startsWith("whsec_"));
    assert.equal(hook.active, true);
    const listed = await client.listWebhooks();
    assert.ok(listed.some((h) => h.id === hook.id));
    assert.ok(listed.every((h) => !("secret" in h)));

    const domain = await client.createDomain({ hostname: "hooks.customer.example", reference: "ws_h" });
    await client.deleteDomain(domain.id);
    const deliveries = await client.listDeliveries(hook.id);
    assert.ok(deliveries.some((d) => d.event_type === "domain.deleted"));
    const pending = await client.listDeliveries(hook.id, { state: "pending" });
    const first = pending[0];
    assert.ok(first);
    assert.equal((await client.replayDelivery(hook.id, first.id)).state, "pending");
    assert.ok((await client.replaySince(hook.id, new Date("2020-01-01T00:00:00Z"))) >= 1);

    const rotated = await client.rotateWebhook(hook.id);
    assert.ok(rotated.secret.startsWith("whsec_"));
    assert.notEqual(rotated.secret, hook.secret);
    assert.ok(rotated.previous_secret_expires_at);

    const revoked = await client.deleteWebhook(hook.id);
    assert.equal(revoked.active, false);
    assert.ok(revoked.revoked_at);
    await assert.rejects(client.rotateWebhook("00000000-0000-0000-0000-000000000000"), NotFoundError);
  });

  test("a wrong credential is refused", async () => {
    const stranger = new CustomDomainClient({ baseUrl: baseUrl ?? "", apiKey: "cd_not_a_real_key" });
    await assert.rejects(stranger.listDomains(), (error) => error instanceof AuthenticationError && error.code === "unauthorized");
  });
});
