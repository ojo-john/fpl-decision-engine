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
 * forwards a JSON body (used by every action button/form). Neither is a
 * generic passthrough beyond that -- there's no PUT/PATCH/DELETE need yet.
 */
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
