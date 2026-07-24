import { test, expect, type Page } from "@playwright/test";
import { login } from "./helpers";

// A real (non-quarantined) FPL squad-select requirement: 2 GKP, 5 DEF, 5 MID, 3 FWD -- the
// LP solver and every position-legality check in the backend enforces exactly this, so a
// squad built any other way (e.g. "first 15 search results") can legitimately be infeasible
// for lineup/H2H solving. See app/jobs/handlers/h2h_simulation.py's own solver-fallback tests
// for the same real-world gotcha, hit for real earlier this session.
const POSITION_COUNTS: Array<[string, number]> = [
  ["GKP", 2],
  ["DEF", 5],
  ["MID", 5],
  ["FWD", 3],
];

async function buildPositionLegalSquad(page: Page) {
  await page.goto("/team/new");
  // The search form is the FIRST <form> on the page -- a second <form> further down (bank/free
  // transfers/active chip) also has its own <select>, so scope to avoid a strict-mode
  // ambiguous-selector error.
  const searchForm = page.locator("form").first();
  for (const [position, count] of POSITION_COUNTS) {
    await searchForm.locator("select").selectOption(position);
    await searchForm.getByRole("button", { name: "Search" }).click();
    const addButtons = page.getByRole("button", { name: "Add", exact: true });
    for (let i = 0; i < count; i++) {
      await addButtons.first().click();
    }
  }
  await expect(page.getByText(/Selected squad \(15\/15\)/)).toBeVisible();
}

test.describe("manual squad lifecycle", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("create, duplicate-and-edit, then delete a manual squad", async ({ page }) => {
    await buildPositionLegalSquad(page);
    await page.click('button:has-text("Create squad")');

    await expect(page).toHaveURL(/\/team\/[0-9a-f-]{36}$/);
    const detailUrl = page.url();
    await expect(page.getByRole("heading", { name: "Manual squad" })).toBeVisible();
    await expect(page.locator("table tbody tr")).toHaveCount(15);
    await expect(page.getByText("Duplicate & edit")).toBeVisible();
    await expect(page.getByText("Delete squad")).toBeVisible();

    // Duplicate & edit must land on a pre-filled editor, not an empty one.
    await page.click("text=Duplicate & edit");
    await expect(page).toHaveURL(/\/team\/new\?from=/);
    await expect(page.getByText(/Selected squad \(15\/15\)/)).toBeVisible();

    // Back to the original, delete it, and confirm it's really gone.
    await page.goto(detailUrl);
    page.once("dialog", (dialog) => dialog.accept());
    await page.click('button:has-text("Delete squad")');

    await expect(page).toHaveURL("/team");
    const response = await page.request.get(`/api/proxy/squads/${detailUrl.split("/team/")[1]}`);
    expect(response.status()).toBe(404);
  });

  test("team landing page offers both sync and build-from-scratch paths", async ({ page }) => {
    await page.goto("/team");

    await expect(page.getByRole("heading", { name: "Sync your team" })).toBeVisible();
    // SingleFieldActionForm's <label> isn't htmlFor-linked to its <input> (a pre-existing,
    // out-of-scope a11y gap), so assert on the visible text + the input's presence separately
    // rather than via getByLabel, which needs that association to resolve.
    await expect(page.getByText("Your FPL manager ID")).toBeVisible();
    await expect(page.locator('input[type="number"]').first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Start from scratch" })).toBeVisible();
    await expect(page.getByText("enter a squad manually")).toBeVisible();
  });
});
