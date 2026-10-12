// Verify and parse webhook deliveries (docs/webhooks.md).
//
// `X-Custom-Domain-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>]`, each
// `v1` an HMAC-SHA256 over `<t>.<raw body>` with one of the subscription's
// secrets. The same checks and error codes as the Python SDK's
// `verify_webhook`; test/fixtures/webhooks.json keeps the two in step.

import { hexDigest, hmacKey, hmacVerify, type Secret, unixNow, utf8 } from "./crypto.js";
import type { WebhookEvent } from "./models.js";

export const SIGNATURE_HEADER = "X-Custom-Domain-Signature";
/** Largest accepted distance between the delivery timestamp and now, in seconds. */
export const DEFAULT_TOLERANCE = 300;

export type SignatureErrorCode = "missing" | "malformed" | "stale" | "bad_signature";

export class SignatureInvalid extends Error {
  readonly code: SignatureErrorCode;

  constructor(code: SignatureErrorCode, message: string) {
    super(message);
    this.name = "SignatureInvalid";
    this.code = code;
  }
}

export interface VerifyWebhookOptions {
  /** Override the clock, in Unix seconds (tests). */
  now?: number;
  /** Seconds a delivery timestamp may differ from now. Default 300. */
  tolerance?: number;
}

/** Python's `int()` on a decimal string: optional sign, digits, single underscores. */
function parseTimestamp(value: string): number | null {
  const text = value.trim();
  if (!/^[+-]?\d+(_\d+)*$/.test(text)) return null;
  // A huge value loses precision but stays far outside the tolerance (stale).
  return Number(text.replace(/_/g, ""));
}

function bodyBytes(body: string | Uint8Array | ArrayBuffer): Uint8Array {
  if (typeof body === "string") return utf8(body);
  return body instanceof Uint8Array ? body : new Uint8Array(body);
}

/**
 * Verify a delivery and return its timestamp, or throw {@link SignatureInvalid}.
 *
 * Pass the **raw** request body, exactly as received (not re-serialized JSON),
 * and every secret you currently hold: the new and the previous one during a
 * rotation.
 */
export async function verifyWebhook(
  header: string | null | undefined,
  body: string | Uint8Array | ArrayBuffer,
  secrets: Iterable<Secret>,
  options: VerifyWebhookOptions = {},
): Promise<number> {
  if (!header) throw new SignatureInvalid("missing", "No signature header");
  let timestamp: number | null = null;
  const given: string[] = [];
  for (const part of header.split(",")) {
    const item = part.trim();
    const at = item.indexOf("=");
    if (at < 0) continue;
    const key = item.slice(0, at);
    const value = item.slice(at + 1);
    if (key === "t") {
      timestamp = parseTimestamp(value);
      if (timestamp === null) throw new SignatureInvalid("malformed", "Timestamp is not an integer");
    } else if (key === "v1") {
      given.push(value);
    }
  }
  if (timestamp === null || given.length === 0) {
    throw new SignatureInvalid("malformed", "Signature header lacks t= or v1=");
  }
  const current = Math.floor(options.now ?? unixNow());
  if (Math.abs(current - timestamp) > (options.tolerance ?? DEFAULT_TOLERANCE)) {
    throw new SignatureInvalid("stale", "Delivery timestamp outside tolerance");
  }
  const raw = bodyBytes(body);
  const prefix = utf8(`${timestamp}.`);
  const signed = new Uint8Array(prefix.length + raw.length);
  signed.set(prefix);
  signed.set(raw, prefix.length);
  const candidates = given.map(hexDigest).filter((d): d is Uint8Array => d !== null);
  for (const secret of secrets) {
    const key = await hmacKey(secret);
    for (const candidate of candidates) {
      if (await hmacVerify(key, candidate, signed)) return timestamp;
    }
  }
  throw new SignatureInvalid("bad_signature", "No signature matched");
}

/**
 * Parse a verified delivery body. Verify first: never act on an unverified
 * body. Deliveries are at least once and may arrive out of order, so
 * deduplicate by `id` and compare `created_at` before moving state.
 */
export function parseEvent(body: string | Uint8Array | ArrayBuffer): WebhookEvent {
  const text = typeof body === "string" ? body : new TextDecoder().decode(bodyBytes(body));
  const data = JSON.parse(text) as WebhookEvent;
  if (
    typeof data !== "object" ||
    data === null ||
    typeof data.id !== "string" ||
    typeof data.type !== "string" ||
    typeof data.created_at !== "string" ||
    typeof data.data?.domain !== "object" ||
    data.data.domain === null
  ) {
    throw new TypeError("Not a webhook event");
  }
  return data;
}
