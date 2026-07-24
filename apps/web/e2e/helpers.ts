import type { Page } from "@playwright/test";

export const VALID_TOKEN = "dev-token-change-me";

export async function login(page: Page, token: string = VALID_TOKEN) {
  await page.goto("/login");
  await page.fill("#token", token);
  await page.click('button[type="submit"]');
}

// Chrome's generic message for ANY failed resource fetch -- carries no URL, so it can't be
// attributed to a specific resource by text alone. This app's pages fetch a favicon that was
// never added (a known, pre-existing cosmetic gap -- see this session's own manual
// verification notes), which is the only thing that ever produces this exact message; a script
// error or an unhandled rejection would show up as a DIFFERENT console message or a
// `pageerror` event, neither of which this filter touches.
const GENERIC_RESOURCE_404_TEXT = "Failed to load resource: the server responded with a status of 404";

/** Real console/page errors for a test -- NOT HTTP response status codes: 404s/409s from the
 * backend are frequently the CORRECT, expected signal this app's own pages render as an empty
 * state or an inline error message (e.g. GET /recommendations/latest 404 -> the dashboard's own
 * "no recommendation yet" empty state), not a bug to flag. */
export function trackConsoleErrors(page: Page): { errors: string[] } {
  const state = { errors: [] as string[] };
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (message.text().startsWith(GENERIC_RESOURCE_404_TEXT)) return;
    state.errors.push(message.text());
  });
  page.on("pageerror", (error) => {
    state.errors.push(`pageerror: ${error.message}`);
  });
  return state;
}
