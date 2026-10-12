import { readFileSync } from "node:fs";

import { base64urlEncode, hmacKey, hmacSign, toHex, utf8 } from "../src/crypto.js";

/** The package root (tests run from build/test/). */
export const ROOT = new URL("../../", import.meta.url);

export function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(new URL(path, ROOT), "utf8")) as T;
}

export const APP = "3f0c8a52-6d1e-4b7a-9c2f-0e1d2c3b4a59";
export const HOST = "forms.customer.example";
export const KEYS = { "1": "test-key-" + "k".repeat(40) };

/** Sign an assertion the way the edge does (for middleware tests with a live clock). */
export async function signAssertion(
  overrides: Partial<{
    keyId: string;
    secret: string;
    app: string;
    ref: string;
    host: string;
    iat: number;
    ttl: number;
  }> = {},
): Promise<string> {
  const keyId = overrides.keyId ?? "1";
  const secret = overrides.secret ?? KEYS["1"];
  const iat = overrides.iat ?? Math.floor(Date.now() / 1000);
  const payload = {
    app: overrides.app ?? APP,
    dom: "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f",
    ref: overrides.ref ?? "ws_8f3a1c",
    host: overrides.host ?? HOST,
    iat,
    exp: iat + (overrides.ttl ?? 60),
    rid: "req-1",
  };
  const encoded = base64urlEncode(utf8(JSON.stringify(payload)));
  const mac = await hmacSign(await hmacKey(secret), utf8(`v1.${keyId}.${encoded}`));
  return `v1.${keyId}.${encoded}.${base64urlEncode(mac)}`;
}

/** Sign a webhook body the way the service does. */
export async function signWebhook(body: string, secrets: string[], timestamp: number) {
  const parts = [`t=${timestamp}`];
  for (const secret of secrets) {
    const mac = await hmacSign(await hmacKey(secret), utf8(`${timestamp}.${body}`));
    parts.push(`v1=${toHex(mac)}`);
  }
  return parts.join(",");
}

/** A domain resource as the API returns it. */
export function domainJson(overrides: Record<string, unknown> = {}) {
  return {
    id: "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f",
    hostname: HOST,
    reference: "ws_8f3a1c",
    status: "pending_dns",
    dns_records: [
      {
        name: `_custom-domain-challenge.${HOST}`,
        type: "TXT",
        value: "custom-domain-verify=abc",
        purpose: "ownership",
        help: "Create a TXT record.",
      },
      {
        name: HOST,
        type: "CNAME",
        value: "acme.edge.example.net",
        purpose: "routing",
        help: "Point the hostname at the target.",
      },
    ],
    checks: ["ownership", "routing", "certificate", "origin"].map((type) => ({
      type,
      status: "pending",
      error_code: null,
      message: null,
      observed_at: null,
      next_check_at: null,
    })),
    metadata: null,
    created_at: "2026-09-25T14:00:00Z",
    updated_at: "2026-09-25T14:00:00Z",
    deleted_at: null,
    ...overrides,
  };
}
