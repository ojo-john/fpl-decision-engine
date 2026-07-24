import { test, expect } from "@playwright/test";
import { login, trackConsoleErrors } from "./helpers";

// The live dev database was freshly re-ingested with the real 2026-27 FPL season (555 real
// players, 20 real teams) -- this suite reads that real data, it does not seed its own.

test.describe("players browser", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("list renders real players", async ({ page }) => {
    const issues = trackConsoleErrors(page);
    await page.goto("/players");

    await expect(page.getByRole("heading", { name: "Players" })).toBeVisible();
    const rows = page.locator("table tbody tr");
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(0);
    expect(issues.errors).toEqual([]);
  });

  test("position filter narrows the list to only that position", async ({ page }) => {
    await page.goto("/players?position=GKP");

    const positionCells = page.locator("table tbody tr td:first-child");
    const count = await positionCells.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < count; i++) {
      await expect(positionCells.nth(i)).toHaveText("GKP");
    }
  });

  test("search narrows results to matching names", async ({ page }) => {
    await page.goto("/players");
    const firstName = await page.locator("table tbody tr td:nth-child(2) a").first().textContent();
    expect(firstName).toBeTruthy();
    const needle = firstName!.slice(0, Math.max(2, Math.floor(firstName!.length / 2)));

    await page.fill('input[name="search"]', needle);
    await page.click('button:has-text("Filter")');

    await expect(page).toHaveURL(new RegExp(`search=${encodeURIComponent(needle)}`));
    const names = await page.locator("table tbody tr td:nth-child(2) a").allTextContents();
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      expect(name.toLowerCase()).toContain(needle.toLowerCase());
    }
  });

  test("opening a player shows its detail page with real stats", async ({ page }) => {
    const issues = trackConsoleErrors(page);
    await page.goto("/players");
    const firstLink = page.locator("table tbody tr td:nth-child(2) a").first();
    const name = await firstLink.textContent();

    await firstLink.click();

    await expect(page.getByRole("heading", { level: 1 })).toContainText(name!.trim());
    await expect(page.getByText("Season stats")).toBeVisible();
    await expect(page.getByText("Total points")).toBeVisible();
    expect(issues.errors).toEqual([]);
  });
});
