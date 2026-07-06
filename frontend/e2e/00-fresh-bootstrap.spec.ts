import { test, expect } from "@playwright/test";
import {
  COMPANY_ID,
  COMPANY_NAME,
  ensureCompany,
} from "./helpers";

/**
 * Smoke happy paths against a freshly-bootstrapped backend.
 *
 * These tests share a single company (`e2eco`) created in the very
 * first test. Subsequent tests assume it's the selected company. The
 * run-e2e.sh script wipes DATA_DIR before every full run, so the
 * "create company" step always sees a clean slate.
 */

test.describe.configure({ mode: "serial" });

test("01 — create a company via the auto-opened dialog (fresh DATA_DIR)", async ({
  page,
}) => {
  await page.goto("/");

  // When no companies exist the CompanySwitcher auto-opens the
  // create-company dialog. Wait for it.
  await expect(page.getByText("Create company")).toBeVisible({
    timeout: 5000,
  });

  // Fill the form.
  await page.getByPlaceholder("e.g. company_a").fill(COMPANY_ID);
  await page.getByPlaceholder("e.g. Company A Pty Ltd").fill(COMPANY_NAME);

  // Submit.
  await page.getByRole("button", { name: /^create$/i }).click();

  // Company switcher should show the new company. This is the
  // authoritative signal that the POST succeeded and React Query
  // refetched — the dialog may take an extra tick to unmount.
  await expect(page.getByLabel("Select company")).toHaveValue(COMPANY_ID, {
    timeout: 10000,
  });
  // Dialog should also be gone after the mutation onSuccess.
  await expect(page.getByText("Create company")).toBeHidden({
    timeout: 5000,
  });
});

test("02 — Dashboard loads with the new company", async ({ page }) => {
  await page.goto("/dashboard");
  // Route-specific heading must appear (sidebar/topbar substrings
  // alone aren't enough — audit P0).
  await expect(page.getByRole("heading", { name: "Dashboard" }))
    .toBeVisible({ timeout: 5000 });
  // And at least one real KPI card must render (proves the dashboard
  // data request resolved, not just the shell).
  await expect(page.getByRole("heading", { name: "Bank accounts" }))
    .toBeVisible({ timeout: 5000 });
  // No error banner.
  await expect(page.locator("text=Failed to load dashboard")).toHaveCount(0);
});

test("03 — Documents page lists Receipts + can open New Receipt dialog", async ({
  page,
}) => {
  await ensureCompany(page.context().request);

  await page.goto("/documents");

  // The page has a "Receipts" section header.
  await expect(page.getByRole("heading", { name: "Receipts" }).first())
    .toBeVisible();

  // And a + Receipt button.
  const newBtn = page.getByRole("button", { name: /\+\s*Receipt/i });
  await expect(newBtn).toBeVisible();

  await newBtn.click();
  // The create dialog's heading must be visible (dialog-scoped, not a
  // loose body-text match that the page could satisfy on its own).
  await expect(
    page.getByRole("heading", { name: "New Receipt" }),
  ).toBeVisible({ timeout: 5000 });
  // The dialog also has an Issue date field; verify it's there as a
  // dialog-scoped sanity check.
  await expect(page.getByLabel("Issue date")).toBeVisible();
});
