import { test, expect } from "@playwright/test";
import { login, trackConsoleErrors } from "./helpers";

/**
 * This suite runs against a SHARED, non-truncated Postgres that other manual verification
 * (and other e2e runs) has already written real rows into over the course of this session --
 * these pages may show either an empty state OR real content depending on what's there right
 * now. Each test asserts "the page rendered ONE of its known-valid states, with no real error",
 * never a specific state, and never an exact count -- mirrors this repo's own backend
 * integration-test discipline for the same shared-database reason (see tests/integration/*.py).
 */

test.describe("page rendering", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("dashboard renders one of its three documented states", async ({ page }) => {
    const issues = trackConsoleErrors(page);
    await page.goto("/");

    // 1. No recommendation_audits row exists at all (404) -- never generated.
    const noneGenerated = page.getByText("No recommendation has been generated for the current season yet.");
    // 2. A recommendation was generated but resolved zero of the 4 correlated components --
    //    a real, current possibility (this is the live dev database's own current state).
    const generatedButNothingResolved = page.getByText(/none of the squad, transfer, H2H, or chip/);
    // 3. A real, at-least-partially-resolved recommendation.
    const realRecommendation = page.getByRole("heading", { name: /recommendation$/ });
    await expect(noneGenerated.or(generatedButNothingResolved).or(realRecommendation)).toBeVisible();
    expect(issues.errors).toEqual([]);
  });

  test("rivals renders either no-league, no-rivals, or a real rivals list", async ({ page }) => {
    const issues = trackConsoleErrors(page);
    await page.goto("/rivals");

    const noLeague = page.getByText("No league has been synced yet.");
    const noRivals = page.getByText(/No rival profiles computed yet/);
    const syncButton = page.getByRole("button", { name: "Sync now" });
    // noRivals and syncButton legitimately render TOGETHER in the "league synced, zero rival
    // profiles yet" state -- .or() alone hits Playwright's strict-mode check when more than one
    // alternative matches at once, so assert "at least one" via .first() instead of requiring
    // the combined locator to resolve to exactly one element.
    await expect(noLeague.or(noRivals).or(syncButton).first()).toBeVisible();
    expect(issues.errors).toEqual([]);
  });

  test("history renders its four sections without crashing", async ({ page }) => {
    const issues = trackConsoleErrors(page);
    await page.goto("/history");

    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Squad & lineup runs" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Transfer runs" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Chip runs" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "H2H runs" })).toBeVisible();
    expect(issues.errors).toEqual([]);
  });

  test("learning renders either no-review or a real review, plus the signals/failures sections", async ({
    page,
  }) => {
    const issues = trackConsoleErrors(page);
    await page.goto("/learning");

    const noReview = page.getByText("No gameweek review has run for the current season yet.");
    const realReview = page.getByRole("heading", { name: /review --/ });
    await expect(noReview.or(realReview)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Learning signals" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "What's gone wrong" })).toBeVisible();
    expect(issues.errors).toEqual([]);
  });
});
