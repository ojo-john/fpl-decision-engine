import { NextRequest, NextResponse } from "next/server";
import {
  GITHUB_CALLBACK_PATH,
  GithubSignInError,
  type GithubSignInErrorCode,
  OAUTH_STATE_COOKIE_NAME,
  parseStateCookie,
  readGithubOAuthConfig,
  verifyGithubSignIn,
} from "@/lib/github-oauth";
import { safeNextPath } from "@/lib/safe-redirect";
import { setSessionCookie } from "@/lib/session";

/**
 * GET /api/auth/github/callback?code=...&state=... -- GitHub redirects here after the user approves.
 *
 * Verifies `state` against the cookie set by /api/auth/github, exchanges `code` for a GitHub
 * token, resolves the user id, checks it against GITHUB_AUTH_ALLOWED_USER_IDS, and only then
 * issues the normal session cookie. Every failure redirects to /login?error=<code> (fails
 * closed -- no session cookie is ever set on a failure path).
 */
export async function GET(request: NextRequest) {
  const startedAt = Date.now();
  const sharedToken = process.env.APP_SHARED_TOKEN;
  if (!sharedToken) {
    throw new Error("Missing required environment variable: APP_SHARED_TOKEN");
  }

  const config = readGithubOAuthConfig(process.env);
  if (!config) return failure(request, "github_not_configured", startedAt);

  const params = request.nextUrl.searchParams;
  const stored = parseStateCookie(request.cookies.get(OAUTH_STATE_COOKIE_NAME)?.value);
  const state = params.get("state");
  if (!stored || !state || stored.state !== state) {
    return failure(request, "github_state_mismatch", startedAt);
  }

  // The user clicked "Cancel" on GitHub's consent screen (or GitHub refused): no code is sent.
  const code = params.get("code");
  if (!code) return failure(request, "github_denied", startedAt);

  const redirectUri = new URL(GITHUB_CALLBACK_PATH, request.nextUrl.origin).toString();
  try {
    const userId = await verifyGithubSignIn(fetch, config, code, redirectUri);
    const response = NextResponse.redirect(new URL(safeNextPath(stored.next), request.url));
    await setSessionCookie(response, sharedToken);
    clearStateCookie(response);
    logOutcome("ok", startedAt, { github_user_id: userId });
    return response;
  } catch (error) {
    if (error instanceof GithubSignInError) return failure(request, error.code, startedAt, error.message);
    throw error;
  }
}

function failure(request: NextRequest, code: GithubSignInErrorCode, startedAt: number, detail?: string) {
  const response = NextResponse.redirect(new URL(`/login?error=${code}`, request.url));
  clearStateCookie(response);
  logOutcome(code, startedAt, detail ? { detail } : {});
  return response;
}

function clearStateCookie(response: NextResponse) {
  response.cookies.set(OAUTH_STATE_COOKIE_NAME, "", { path: "/api/auth/github", maxAge: 0 });
}

function logOutcome(outcome: string, startedAt: number, extra: Record<string, unknown>) {
  // Structured, one line per sign-in attempt: outcome doubles as the error-type dimension.
  // Never logs the OAuth code, the GitHub token, or the session cookie.
  const line = JSON.stringify({
    event: "github_sign_in",
    outcome,
    latency_ms: Date.now() - startedAt,
    ...extra,
  });
  if (outcome === "ok") console.info(line);
  else console.warn(line);
}
