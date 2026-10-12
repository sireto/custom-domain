// HMAC-SHA256 and encodings on Web Crypto, so the same code runs in Node 20+,
// Deno, Bun, browsers and edge runtimes (Cloudflare Workers, Vercel Edge).

const encoder = new TextEncoder();

/** A secret as given by the operator: text (encoded as UTF-8) or raw bytes. */
export type Secret = string | Uint8Array;

function subtle(): SubtleCrypto {
  const value = globalThis.crypto?.subtle;
  if (!value) {
    throw new Error("Web Crypto (globalThis.crypto.subtle) is required; use Node 20 or later");
  }
  return value;
}

export function utf8(text: string): Uint8Array {
  return encoder.encode(text);
}

function bytes(data: Uint8Array): BufferSource {
  // Web Crypto wants an ArrayBuffer-backed view; copy anything else.
  return (data.buffer instanceof ArrayBuffer ? data : new Uint8Array(data)) as BufferSource;
}

export async function hmacKey(secret: Secret): Promise<CryptoKey> {
  const raw = bytes(typeof secret === "string" ? utf8(secret) : secret);
  return subtle().importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
    "verify",
  ]);
}

/**
 * Whether `signature` is the HMAC-SHA256 of `data` under `key`. Web Crypto's
 * verify compares in constant time.
 */
export async function hmacVerify(
  key: CryptoKey,
  signature: Uint8Array,
  data: Uint8Array,
): Promise<boolean> {
  return subtle().verify("HMAC", key, bytes(signature), bytes(data));
}

export async function hmacSign(key: CryptoKey, data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await subtle().sign("HMAC", key, bytes(data)));
}

const B64_ALPHABET = /[A-Za-z0-9+/]/;

/**
 * Decode base64url exactly as Python's `base64.urlsafe_b64decode(text +
 * padding)` does, which is what the Python SDK and the service use: `-_` map
 * to `+/`, other characters outside the alphabet are skipped, and decoding
 * stops at a complete padding sequence. `null` where Python raises (non-ASCII
 * text, a dangling character, incorrect padding).
 */
export function base64urlDecode(text: string): Uint8Array | null {
  // Non-ASCII text is refused, as Python refuses it.
  if (!/^[\x00-\x7f]*$/.test(text)) return null;
  const padded = (text + "=".repeat((4 - (text.length % 4)) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  // CPython's non-strict a2b_base64.
  let data = "";
  let quad = 0;
  let pads = 0;
  let complete = false;
  for (const ch of padded) {
    if (ch === "=") {
      // `quad_pos >= 2 && quad_pos + ++pads >= 4`: pads only count after two characters.
      if (quad >= 2) {
        pads += 1;
        if (quad + pads >= 4) {
          complete = true;
          break;
        }
      }
      continue;
    }
    if (!B64_ALPHABET.test(ch)) continue;
    pads = 0;
    data += ch;
    quad = (quad + 1) % 4;
  }
  if (!complete && quad !== 0) return null;
  let binary: string;
  try {
    binary = atob(data);
  } catch {
    return null;
  }
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

export function base64urlEncode(data: Uint8Array): string {
  let binary = "";
  for (const byte of data) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const LOWER_HEX_SHA256 = /^[0-9a-f]{64}$/;

/**
 * Decode a lowercase hex HMAC-SHA256 digest, as the service writes them;
 * `null` for anything else (which can never match a signature).
 */
export function hexDigest(text: string): Uint8Array | null {
  if (!LOWER_HEX_SHA256.test(text)) return null;
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export function toHex(data: Uint8Array): string {
  let out = "";
  for (const byte of data) out += byte.toString(16).padStart(2, "0");
  return out;
}

/** Whole seconds since the epoch. */
export function unixNow(): number {
  return Math.floor(Date.now() / 1000);
}
