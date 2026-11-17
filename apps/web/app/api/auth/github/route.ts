import { NextRequest, NextResponse } from "next/server";
import {
  buildAuthorizeUrl,
  GITHUB_CALLBACK_PATH,
  OAUTH_STATE_COOKIE_NAME,
  OAUTH_STATE_MAX_AGE_SECONDS,
  readGithubOAuthConfig,
  serializeStateCookie,
} from "@/lib/github-oauth";
import { safeNextPath } from "@/lib/safe-redirect";

/**
 * GET /api/auth/github?next=/some/page -- starts "Sign in with GitHub".
 *
 * Generates a random `state`, stores it (plus the post-login redirect target) in a short-lived
 * httpOnly cookie, and redirects to GitHub's authorize page. The callback rejects any response
 * whose `state` doesn't match that cookie, which is what stops a forged callback (login CSRF).
 */
export async function GET(request: NextRequest) {
  const config = readGithubOAuthConfig(process.env);
  if (!config) {
    return NextResponse.redirect(new URL("/login?error=github_not_configured", request.url));
  }

  const state = crypto.randomUUID();
  const next = safeNextPath(request.nextUrl.searchParams.get("next"));
  const redirectUri = new URL(GITHUB_CALLBACK_PATH, request.nextUrl.origin).toString();

  const response = NextResponse.redirect(buildAuthorizeUrl(config.clientId, redirectUri, state));
  response.cookies.set(OAUTH_STATE_COOKIE_NAME, serializeStateCookie({ state, next }), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    // lax (not strict): the cookie must ride along on GitHub's top-level redirect back to us.
    sameSite: "lax",
    maxAge: OAUTH_STATE_MAX_AGE_SECONDS,
    path: "/api/auth/github",
  });
  return response;
}
