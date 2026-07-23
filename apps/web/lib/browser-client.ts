import createClient from "openapi-fetch";
import type { paths } from "./api-types";

/**
 * Client-side counterpart to lib/api-client.ts's backendClient() -- same
 * generated `paths` types, but hits this app's own /api/proxy instead of
 * the backend directly (the browser never holds APP_SHARED_TOKEN; see that
 * route's own docstring). Only import this from "use client" components.
 */
export function browserClient() {
  return createClient<paths>({ baseUrl: "/api/proxy" });
}
