import { expect, test, type Page } from "@playwright/test";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
} from "./helpers";

const COMPANY_ID = "periodlockui";
const LOCK_DATE = "2024-06-30";

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
  await expect(switcher).toHaveValue(COMPANY_ID);
}

test("Settings confirms an irreversible forward-only accounting period lock", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Period Lock UI Pty Ltd");

  await page.goto("/settings");
  await selectCompany(page);
  await expect(page.getByText("Books are open - no period has been locked")).toBeVisible();

  const dateInput = page.getByLabel("Lock through date");
  const reviewButton = page.getByRole("button", {
    name: "Review irreversible lock",
  });
  await expect(dateInput).toHaveAttribute("max", /^\d{4}-\d{2}-\d{2}$/);
  await expect(reviewButton).toBeDisabled();
  await dateInput.fill(LOCK_DATE);
  await expect(reviewButton).toBeEnabled();

  const companyPattern = `**/api/v1/companies/${COMPANY_ID}`;
  let rejectOnce = true;
  await page.route(companyPattern, async (route) => {
    if (route.request().method() === "PATCH" && rejectOnce) {
      rejectOnce = false;
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "The accounting period lock can only move forward (simulated conflict).",
        }),
      });
      return;
    }
    await route.continue();
  });

  await reviewButton.click();
  const confirmation = page.getByRole("heading", {
    name: "Permanently lock this accounting period?",
  });
  await expect(confirmation).toBeVisible();
  await expect(page.getByText("This is irreversible", { exact: false })).toBeVisible();
  await expect(page.getByText("adjustments in an open period", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Lock period permanently" }).click();
  await expect(
    page.getByText("The accounting period lock can only move forward (simulated conflict)."),
  ).toBeVisible();

  await page.unroute(companyPattern);
  await reviewButton.click();
  await page.getByRole("button", { name: "Lock period permanently" }).click();
  await expect(page.getByText("Books locked through 30/06/2024")).toBeVisible();
  await expect(
    page.getByText("Accounting periods are now locked through 30/06/2024."),
  ).toBeVisible();

  const companyResponse = await request.get(
    `${BACKEND_URL}/api/v1/companies/${COMPANY_ID}`,
    { headers: companyHeaders(COMPANY_ID) },
  );
  expect(companyResponse.ok()).toBeTruthy();
  expect(await companyResponse.json()).toMatchObject({
    books_locked_through: LOCK_DATE,
  });

  await dateInput.fill("2024-06-29");
  await expect(page.getByText("The lock cannot move backward from 30/06/2024.")).toBeVisible();
  await expect(reviewButton).toBeDisabled();

  await dateInput.fill(LOCK_DATE);
  await expect(reviewButton).toBeEnabled();
  await dateInput.fill("");
  await expect(reviewButton).toBeDisabled();
});
