import assert from "node:assert/strict";
import { test } from "node:test";

import { isReady, parseEvent, SignatureInvalid, verifyWebhook } from "../src/index.js";
import { domainJson, signWebhook } from "./helpers.js";

const body = JSON.stringify({
  id: "e1",
  type: "domain.ready",
  created_at: "2026-09-25T15:00:00+00:00",
  data: { domain: domainJson({ status: "ready" }) },
});

test("verifies with the live clock and parses the event", async () => {
  const now = Math.floor(Date.now() / 1000);
  const header = await signWebhook(body, ["whsec_old", "whsec_new"], now);
  assert.equal(await verifyWebhook(header, body, ["whsec_new"]), now);
  assert.equal(await verifyWebhook(header, new TextEncoder().encode(body).buffer, ["whsec_old"]), now);
  await assert.rejects(verifyWebhook(header, body, ["whsec_other"]), (error) => {
    assert.ok(error instanceof SignatureInvalid);
    assert.equal(error.code, "bad_signature");
    return true;
  });
  const event = parseEvent(body);
  assert.equal(event.type, "domain.ready");
  assert.equal(event.id, "e1");
  assert.ok(isReady(event.data.domain));
  assert.equal(parseEvent(new TextEncoder().encode(body)).data.domain.hostname, "forms.customer.example");
});

test("parseEvent refuses something that is not an event", () => {
  assert.throws(() => parseEvent("{}"), TypeError);
  assert.throws(() => parseEvent('{"id":"e","type":"t","created_at":"x","data":{}}'), TypeError);
  assert.throws(() => parseEvent("not json"), SyntaxError);
});
