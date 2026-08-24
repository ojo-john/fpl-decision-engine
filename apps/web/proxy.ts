import { NextRequest, NextResponse } from "next/server";
import { isValidSessionCookieValue, SESSION_COOKIE_NAME } from "@/lib/session";

// Gates every route except /login and /api/session behind the signed session
// cookie (Milestone 8 Layer 1/2). Named `proxy.ts` per Next.js 16's rename of
// the "Middleware" file convention -- same mechanism, new name.
const PUBLIC_PATHS = ["/login", "/api/session"];

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`))) {
    return NextResponse.next();
  }

  const expected = process.env.APP_SHARED_TOKEN;
  if (!expected) {
    throw new Error("Missing required environment variable: APP_SHARED_TOKEN");
  }

  const cookieValue = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (await isValidSessionCookieValue(cookieValue, expected)) {
    return NextResponse.next();
  }

  const loginUrl = new URL("/login", request.url);
  loginUrl.searchParams.set("next", pathname);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  // Every request except Next.js internals and static assets -- an
  // unauthenticated request to any page or API route must hit this check.
  // manifest.json/sw.js/icons/ (Milestone 9 Layer 1, PWA push support) must also stay excluded:
  // the browser fetches these directly (manifest link, service worker registration) without ever
  // going through this app's own login flow, and a login-page redirect breaks both -- a real bug
  // caught locally (2026-08-23): /sw.js returned this middleware's 307 redirect to /login instead
  // of the real script, which would have made every service worker registration fail outright.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|manifest.json|sw.js|icons/).*)"],
};
