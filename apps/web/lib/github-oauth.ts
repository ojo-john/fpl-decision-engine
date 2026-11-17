/**
 * "Sign in with GitHub" helpers (GitHub OAuth App, web flow).
 *
 * An alternative to typing APP_SHARED_TOKEN on /login: the user proves who they are to GitHub,
 * and if their numeric GitHub user id is in GITHUB_AUTH_ALLOWED_USER_IDS this app issues the
 * exact same signed session cookie the token login does (lib/session.ts). Nothing downstream
 * changes -- the browser still never holds the backend token, and apps/api is unaffected.
 *
 * Identity is the numeric user id, never the login (a login can be renamed and re-registered).
 * Requests no OAuth scopes: GET /user with an unscoped token returns the public profile, which
 * is all that's needed, and the GitHub token is discarded right after that one call.
 *
 * Every function here takes its `fetch` as a parameter so it is unit-testable without a network.
 */

export const OAUTH_STATE_COOKIE_NAME = "fpl_oauth_state";
export const OAUTH_STATE_MAX_AGE_SECONDS = 10 * 60;
export const GITHUB_CALLBACK_PATH = "/api/auth/github/callback";

const GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize";
const GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token";
const GITHUB_USER_URL = "https://api.github.com/user";
// Explicit timeout on every GitHub call -- never rely on fetch's (absent) default.
const GITHUB_REQUEST_TIMEOUT_MS = 10_000;

export type FetchFn = typeof fetch;

/** Why a GitHub sign-in failed; surfaced to /login as `?error=<code>`. */
export type GithubSignInErrorCode =
  | "github_not_configured"
  | "github_state_mismatch"
  | "github_denied"
  | "github_unavailable"
  | "github_not_allowed";

export class GithubSignInError extends Error {
  constructor(
    readonly code: GithubSignInErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "GithubSignInError";
  }
}

export interface GithubOAuthConfig {
  clientId: string;
  clientSecret: string;
  allowedUserIds: ReadonlySet<number>;
}

/**
 * Reads the OAuth config from env. Returns null when GitHub sign-in isn't configured (the login
 * page then simply doesn't offer it). Throws on a malformed allowlist entry -- a typo should fail
 * loudly, not silently allow nobody.
 */
export function readGithubOAuthConfig(env: Record<string, string | undefined>): GithubOAuthConfig | null {
  const clientId = env.GITHUB_OAUTH_CLIENT_ID?.trim();
  const clientSecret = env.GITHUB_OAUTH_CLIENT_SECRET?.trim();
  const allowedUserIds = parseAllowedUserIds(env.GITHUB_AUTH_ALLOWED_USER_IDS ?? "");
  if (!clientId || !clientSecret || allowedUserIds.size === 0) return null;
  return { clientId, clientSecret, allowedUserIds };
}

export function parseAllowedUserIds(raw: string): ReadonlySet<number> {
  const ids = new Set<number>();
  for (const part of raw.split(",")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    if (!/^\d+$/.test(trimmed)) {
      throw new Error(`GITHUB_AUTH_ALLOWED_USER_IDS contains a non-numeric entry: "${trimmed}"`);
    }
    ids.add(Number(trimmed));
  }
  return ids;
}

export function buildAuthorizeUrl(clientId: string, redirectUri: string, state: string): string {
  const url = new URL(GITHUB_AUTHORIZE_URL);
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("state", state);
  url.searchParams.set("allow_signup", "false");
  return url.toString();
}

export interface OAuthStateCookie {
  state: string;
  next: string;
}

export function serializeStateCookie(value: OAuthStateCookie): string {
  return JSON.stringify(value);
}

export function parseStateCookie(raw: string | undefined): OAuthStateCookie | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as OAuthStateCookie).state === "string" &&
      typeof (parsed as OAuthStateCookie).next === "string"
    ) {
      return { state: (parsed as OAuthStateCookie).state, next: (parsed as OAuthStateCookie).next };
    }
    return null;
  } catch {
    // Tampered/garbled cookie: treated exactly like a missing one (state mismatch), not an error.
    return null;
  }
}

async function githubFetch(fetchFn: FetchFn, url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetchFn(url, { ...init, signal: AbortSignal.timeout(GITHUB_REQUEST_TIMEOUT_MS) });
  } catch (error) {
    // Network failure or timeout (AbortSignal.timeout rejects with a TimeoutError DOMException).
    throw new GithubSignInError("github_unavailable", `GitHub request failed: ${(error as Error).name}`);
  }
}

/** Exchanges the OAuth `code` for a GitHub access token. */
export async function exchangeCodeForToken(
  fetchFn: FetchFn,
  config: GithubOAuthConfig,
  code: string,
  redirectUri: string,
): Promise<string> {
  const response = await githubFetch(fetchFn, GITHUB_TOKEN_URL, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: config.clientId,
      client_secret: config.clientSecret,
      code,
      redirect_uri: redirectUri,
    }),
  });
  if (!response.ok) {
    throw new GithubSignInError("github_unavailable", `GitHub token exchange returned ${response.status}`);
  }
  const body = (await response.json().catch(() => null)) as { access_token?: unknown; error?: unknown } | null;
  // GitHub reports a bad/expired/reused code as HTTP 200 with an `error` field, not a 4xx.
  if (typeof body?.access_token === "string" && body.access_token) return body.access_token;
  if (typeof body?.error === "string") {
    throw new GithubSignInError("github_denied", `GitHub token exchange error: ${body.error}`);
  }
  throw new GithubSignInError("github_unavailable", "GitHub token exchange returned a malformed body");
}

/** Resolves a GitHub access token to its owner's numeric user id. */
export async function fetchGithubUserId(fetchFn: FetchFn, accessToken: string): Promise<number> {
  const response = await githubFetch(fetchFn, GITHUB_USER_URL, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (response.status === 401 || response.status === 403) {
    throw new GithubSignInError("github_denied", `GitHub /user rejected the token (${response.status})`);
  }
  if (!response.ok) {
    throw new GithubSignInError("github_unavailable", `GitHub /user returned ${response.status}`);
  }
  const body = (await response.json().catch(() => null)) as { id?: unknown } | null;
  if (typeof body?.id !== "number" || !Number.isInteger(body.id)) {
    throw new GithubSignInError("github_unavailable", "GitHub /user returned a malformed body");
  }
  return body.id;
}

/** Full callback verification: code -> token -> user id -> allowlist. Returns the allowed user id. */
export async function verifyGithubSignIn(
  fetchFn: FetchFn,
  config: GithubOAuthConfig,
  code: string,
  redirectUri: string,
): Promise<number> {
  const accessToken = await exchangeCodeForToken(fetchFn, config, code, redirectUri);
  const userId = await fetchGithubUserId(fetchFn, accessToken);
  if (!config.allowedUserIds.has(userId)) {
    throw new GithubSignInError("github_not_allowed", `GitHub user ${userId} is not allowlisted`);
  }
  return userId;
}
