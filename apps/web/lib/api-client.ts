import createClient from "openapi-fetch";
import type { paths } from "./api-types";

/**
 * Server-only backend client -- imported exclusively by Server Components
 * and Route Handlers, never by a "use client" component. Talks directly to
 * the FastAPI backend using the real shared token (Settings.app_shared_token
 * on the backend side); the browser never sees this token, only the signed
 * session cookie set at login (see lib/session.ts).
 */
function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export function backendClient() {
  return createClient<paths>({
    baseUrl: requireEnv("API_BASE_URL"),
    headers: {
      Authorization: `Bearer ${requireEnv("APP_SHARED_TOKEN")}`,
    },
  });
}
