import { NextRequest, NextResponse } from "next/server";
import {
  createSessionCookieValue,
  isCorrectSharedToken,
  SESSION_COOKIE_NAME,
  SESSION_MAX_AGE_SECONDS,
} from "@/lib/session";

/** POST /api/session: log in with the shared token, set the signed session cookie. */
export async function POST(request: NextRequest) {
  const body = (await request.json().catch(() => null)) as { token?: unknown } | null;
  const submitted = typeof body?.token === "string" ? body.token : "";

  const expected = process.env.APP_SHARED_TOKEN;
  if (!expected) {
    throw new Error("Missing required environment variable: APP_SHARED_TOKEN");
  }

  if (!submitted || !isCorrectSharedToken(submitted, expected)) {
    return NextResponse.json({ error: "Incorrect token." }, { status: 401 });
  }

  const cookieValue = await createSessionCookieValue(expected);
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE_NAME, cookieValue, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: SESSION_MAX_AGE_SECONDS,
    path: "/",
  });
  return response;
}

/** DELETE /api/session: log out by clearing the session cookie. */
export async function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.delete(SESSION_COOKIE_NAME);
  return response;
}
