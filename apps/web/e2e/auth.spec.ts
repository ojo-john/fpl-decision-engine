import { test, expect } from "@playwright/test";
import { login, VALID_TOKEN } from "./helpers";

test.describe("auth", () => {
  test("wrong token shows an error and does not log in", async ({ page }) => {
    await login(page, "definitely-not-the-real-token");

    await expect(page.getByText("Incorrect token.")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("correct token logs in and redirects to the dashboard", async ({ page }) => {
    await login(page, VALID_TOKEN);

    await expect(page).toHaveURL("/");
    await expect(page.getByRole("navigation")).toContainText("FPL Decision Engine");
  });

  test("log out returns to the login page and re-guards the app", async ({ page }) => {
    await login(page, VALID_TOKEN);
    await expect(page).toHaveURL("/");

    await page.click('button:has-text("Log out")');
    await expect(page).toHaveURL(/\/login/);

    // The proxy.ts session gate must actually re-block a protected page after logout, not
    // just redirect the logout button's own click.
    await page.goto("/team");
    await expect(page).toHaveURL(/\/login/);
  });
});
