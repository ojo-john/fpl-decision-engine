import { NextRequest, NextResponse } from "next/server";

/**
 * Authenticated reverse proxy to the FastAPI backend (Milestone 8 Layer 3).
 *
 * Read-only Server Component pages call the backend directly (see
 * lib/api-client.ts) -- they run entirely server-side already. This route
 * exists ONLY for client-side interactivity (buttons/forms and the player
 * search picker in "use client" components): the browser can't safely hold
 * APP_SHARED_TOKEN, so it calls this same-origin route instead, which
 * attaches the real token server-side and forwards the request. proxy.ts
 * already gates this whole path behind the signed session cookie (it's not
 * in PUBLIC_PATHS), so no separate auth check is duplicated here.
 *
 * GET is read-only passthrough (used by the player-search picker); POST
 * forwards a JSON body (used by every action button/form); DELETE forwards
 * with no body (used by the manual-squad delete button). Not a generic
 * passthrough beyond that -- there's no PUT/PATCH need yet.
 */
// Real, non-obvious risk found building the joint chip scheduler's Phase 2 (POST
// /optimize/chips/schedule): unlike every other action in this app (seconds, not minutes), that
// endpoint's own synchronous evaluation can genuinely take several real minutes. Without this,
// Vercel's own default serverless function duration (10s on the Hobby plan) would kill THIS
// route's own response well before the backend finishes -- the backend job itself keeps running
// and still completes/persists server-side regardless (this proxy killing its own response has no
// effect on the backend), but the button's own inline success state would never see it. Raised to
// the real ceiling this app might plausibly need; Vercel silently caps this at whatever the
// account's own real plan actually allows, so this is a safe, honest attempt, not a guarantee --
// a run that genuinely exceeds the account's real limit still fails from the frontend's own
// perspective even though the backend run itself succeeds and remains visible via
// GET /optimize/chips/schedule's own history.
export const maxDuration = 300;

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

async function forward(request: NextRequest, path: string[], init: RequestInit) {
  const targetUrl = `${requireEnv("API_BASE_URL")}/${path.join("/")}${request.nextUrl.search}`;

  const backendResponse = await fetch(targetUrl, {
    ...init,
    headers: {
      ...init.headers,
      Authorization: `Bearer ${requireEnv("APP_SHARED_TOKEN")}`,
    },
  });

  // A 204 must not carry a body -- passing one through (even an empty string) is invalid
  // per the HTTP spec and NextResponse enforces that.
  if (backendResponse.status === 204) {
    return new NextResponse(null, { status: 204 });
  }
  const responseBody = await backendResponse.text();
  return new NextResponse(responseBody, {
    status: backendResponse.status,
    headers: { "Content-Type": backendResponse.headers.get("Content-Type") ?? "application/json" },
  });
}

export async function GET(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  return forward(request, path, { method: "GET" });
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  const body = await request.text();
  return forward(request, path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  return forward(request, path, { method: "DELETE" });
}
