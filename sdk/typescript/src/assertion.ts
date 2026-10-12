// Verify the edge-signed workspace assertion (docs/edge-routing.md).
//
// Format: `v1.<key id>.<base64url payload>.<base64url HMAC-SHA256>`, the MAC
// covering `v1.<key id>.<payload>`. The same algorithm, checks and error codes
// as the Python SDK's `verify_assertion`; test/fixtures/assertions.json, made
// by the Python code, keeps the two in step.

import { base64urlDecode, hmacKey, hmacVerify, type Secret, unixNow, utf8 } from "./crypto.js";

export const ASSERTION_HEADER = "X-Custom-Domain-Assertion";
export const ASSERTION_VERSION = "v1";
/** Clock skew allowed on `iat` and `exp`, in seconds. */
export const DEFAULT_SKEW = 30;

/** Why an assertion was refused; the same codes as the Python SDK. */
export type AssertionErrorCode =
  | "missing"
  | "malformed"
  | "unknown_key"
  | "bad_signature"
  | "not_yet_valid"
  | "expired"
  | "wrong_application"
  | "wrong_hostname";

export class AssertionInvalid extends Error {
  readonly code: AssertionErrorCode;

  constructor(code: AssertionErrorCode, message: string) {
    super(message);
    this.name = "AssertionInvalid";
    this.code = code;
  }
}

/** The signed payload as the edge writes it. */
export interface AssertionPayload {
  /** Application id the edge routed for. */
  app: string;
  /** Domain id. */
  dom: string;
  /** The workspace reference registered with the domain: select the tenant by this. */
  ref: string;
  /** Canonical customer hostname. */
  host: string;
  /** Issued at, Unix seconds. */
  iat: number;
  /** Expires at, Unix seconds. */
  exp: number;
  /** The edge's request id. */
  rid: string;
}

/** A verified assertion. */
export interface Assertion {
  applicationId: string;
  domainId: string;
  /** The workspace reference: the only value to select the tenant by. */
  reference: string;
  hostname: string;
  issuedAt: number;
  expiresAt: number;
  requestId: string;
  /** Which of your keys verified it. */
  keyId: string;
  /** The payload fields as signed (`app`, `dom`, `ref`, `host`, `iat`, `exp`, `rid`). */
  payload: AssertionPayload;
}

/**
 * Verification keys by key id: the current and the previous key during a
 * rotation, and an application's own `app_…` keys next to the deployment key.
 */
export type AssertionKeys = Readonly<Record<string, Secret>> | ReadonlyMap<string, Secret>;

export interface VerifyAssertionOptions {
  /** Your application's id, as given by the operator. Always checked. */
  applicationId: string;
  /**
   * The hostname the request arrived for (`Host`, or `X-Forwarded-Host` in
   * `origin` mode). Compared case-insensitively, ignoring a trailing dot.
   * Omit only when you check the hostname yourself.
   */
  hostname?: string | null;
  /** Override the clock, in Unix seconds (tests). */
  now?: number;
  /** Allowed clock skew in seconds. Default 30. */
  skew?: number;
}

function lookupKey(keys: AssertionKeys, keyId: string): Secret | undefined {
  if (keys instanceof Map) return (keys as ReadonlyMap<string, Secret>).get(keyId);
  // Own properties only: a key id such as "constructor" must not find Object's.
  const record = keys as Readonly<Record<string, Secret>>;
  return Object.prototype.hasOwnProperty.call(record, keyId) ? record[keyId] : undefined;
}

function asString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  throw new Error("not a string");
}

function asInt(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string" && /^\s*[+-]?\d+\s*$/.test(value)) return Number.parseInt(value, 10);
  throw new Error("not an integer");
}

function decodePayload(text: string): AssertionPayload {
  const raw = base64urlDecode(text);
  if (raw === null) throw new Error("not base64url");
  const data: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new Error("not an object");
  }
  const d = data as Record<string, unknown>;
  return {
    app: asString(d.app),
    dom: asString(d.dom),
    ref: asString(d.ref),
    host: asString(d.host),
    iat: asInt(d.iat),
    exp: asInt(d.exp),
    rid: asString(d.rid),
  };
}

/**
 * Verify an `X-Custom-Domain-Assertion` value and return the assertion, or
 * throw {@link AssertionInvalid} with a stable `code`.
 */
export async function verifyAssertion(
  token: string | null | undefined,
  keys: AssertionKeys,
  options: VerifyAssertionOptions,
): Promise<Assertion> {
  if (!token) throw new AssertionInvalid("missing", "No assertion header");
  const parts = token.split(".");
  if (parts.length !== 4 || parts[0] !== ASSERTION_VERSION) {
    throw new AssertionInvalid("malformed", "Assertion is not a v1 token");
  }
  const [, keyId, payloadText, signatureText] = parts as [string, string, string, string];
  const secret = lookupKey(keys, keyId);
  if (secret === undefined) {
    throw new AssertionInvalid(
      "unknown_key",
      `Assertion signed with unknown key id ${JSON.stringify(keyId)}`,
    );
  }
  const signature = base64urlDecode(signatureText);
  if (signature === null) {
    throw new AssertionInvalid("malformed", "Assertion signature is not base64url");
  }
  const signed = utf8(`${ASSERTION_VERSION}.${keyId}.${payloadText}`);
  if (!(await hmacVerify(await hmacKey(secret), signature, signed))) {
    throw new AssertionInvalid("bad_signature", "Assertion signature does not verify");
  }
  let payload: AssertionPayload;
  try {
    payload = decodePayload(payloadText);
  } catch {
    throw new AssertionInvalid("malformed", "Assertion payload is not valid");
  }
  const skew = options.skew ?? DEFAULT_SKEW;
  const current = Math.floor(options.now ?? unixNow());
  if (payload.iat > current + skew) {
    throw new AssertionInvalid("not_yet_valid", "Assertion issued in the future");
  }
  if (payload.exp + skew < current) {
    throw new AssertionInvalid("expired", "Assertion has expired");
  }
  if (payload.app !== options.applicationId) {
    throw new AssertionInvalid("wrong_application", "Assertion is for another application");
  }
  if (
    options.hostname !== undefined &&
    options.hostname !== null &&
    payload.host !== options.hostname.toLowerCase().replace(/\.+$/, "")
  ) {
    throw new AssertionInvalid("wrong_hostname", "Assertion is for another hostname");
  }
  return {
    applicationId: payload.app,
    domainId: payload.dom,
    reference: payload.ref,
    hostname: payload.host,
    issuedAt: payload.iat,
    expiresAt: payload.exp,
    requestId: payload.rid,
    keyId,
    payload,
  };
}
