import { NextRequest, NextResponse } from "next/server";

/**
 * Authenticated reverse proxy to the FastAPI backend (Milestone 8 Layer 3).
 *
 * Read-only Server Component pages call the backend directly (see
 * lib/api-client.ts) -- they run entirely server-side already. This route
 * exists ONLY for client-side interactive actions (buttons/forms in "use
 * client" components): the browser can't safely hold APP_SHARED_TOKEN, so it
 * calls this same-origin route instead, which attaches the real token
 * server-side and forwards the request. proxy.ts already gates this whole
 * path behind the signed session cookie (it's not in PUBLIC_PATHS), so no
 * separate auth check is duplicated here.
 */
function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  const targetUrl = `${requireEnv("API_BASE_URL")}/${path.join("/")}`;

  const body = await request.text();
  const backendResponse = await fetch(targetUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${requireEnv("APP_SHARED_TOKEN")}`,
    },
    body,
  });

  const responseBody = await backendResponse.text();
  return new NextResponse(responseBody, {
    status: backendResponse.status,
    headers: { "Content-Type": backendResponse.headers.get("Content-Type") ?? "application/json" },
  });
}
