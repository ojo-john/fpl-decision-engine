/**
 * Signed session-cookie helpers (Milestone 8 Layer 1/2).
 *
 * No session store, no per-user identity -- this is a single-manager system
 * with one shared secret (see apps/api/auth.py's own docstring for why).
 * The cookie proves "this browser passed the login check", nothing more: its
 * value is `${issuedAtSeconds}.${hmacHex}`, HMAC-SHA256'd over the timestamp
 * keyed by APP_SHARED_TOKEN. Deliberately does NOT carry the raw shared
 * token itself -- callers needing to talk to the backend read
 * APP_SHARED_TOKEN directly from the server-only environment (see
 * lib/api-client.ts), not from this cookie.
 *
 * Built on the standard Web Crypto API (`crypto.subtle`), not Node's
 * `crypto` module, so this works unchanged in both the Node and Edge
 * runtimes Next.js middleware can run under.
 */

export const SESSION_COOKIE_NAME = "fpl_session";
export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30; // 30 days

function bufferToHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function sign(message: string, secret: string): Promise<string> {
  const key = await hmacKey(secret);
  const signatureBuffer = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return bufferToHex(signatureBuffer);
}

export async function createSessionCookieValue(secret: string): Promise<string> {
  const issuedAt = Math.floor(Date.now() / 1000).toString();
  const signature = await sign(issuedAt, secret);
  return `${issuedAt}.${signature}`;
}

export async function isValidSessionCookieValue(
  value: string | undefined,
  secret: string,
): Promise<boolean> {
  if (!value) return false;
  const [issuedAt, signature] = value.split(".");
  if (!issuedAt || !signature) return false;

  const issuedAtSeconds = Number(issuedAt);
  if (!Number.isFinite(issuedAtSeconds)) return false;
  const ageSeconds = Math.floor(Date.now() / 1000) - issuedAtSeconds;
  if (ageSeconds < 0 || ageSeconds > SESSION_MAX_AGE_SECONDS) return false;

  const expectedSignature = await sign(issuedAt, secret);
  return timingSafeEqual(signature, expectedSignature);
}

/** Same constant-time-ish comparison used for the login form's password check. */
export function isCorrectSharedToken(submitted: string, expected: string): boolean {
  return timingSafeEqual(submitted, expected);
}
