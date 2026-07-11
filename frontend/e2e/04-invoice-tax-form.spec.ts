import { expect, test, type Page } from "@playwright/test";
import {
  EMPTY_FORM,
  toCreatePayload,
  type InvoiceFormValues,
} from "../src/components/invoices/InvoiceForm";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
} from "./helpers";

const COMPANY_ID = "invoicetaxui";

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
}

function values(overrides: Partial<InvoiceFormValues>): InvoiceFormValues {
  return {
    ...EMPTY_FORM,
    contact_name: "Tax Test",
    invoice_number: "TAX-1",
    issue_date: "2024-07-01",
    account_id: 42,
    subtotal: "100.00",
    gst_amount: "0",
    total: "100.00",
    ...overrides,
  };
}

test("invoice line payload carries explicit AU tax treatment", () => {
  const positiveGst = toCreatePayload(
    values({ gst_amount: "10.00", total: "110.00", tax_code: "gst_free" }),
    { gst_registered: true },
  );
  expect(positiveGst.lines?.[0]).toMatchObject({
    line_gst: "10.00",
    tax_code: "standard",
  });

  const gstFree = toCreatePayload(values({ tax_code: "gst_free" }), {
    gst_registered: true,
  });
  expect(gstFree.lines?.[0].tax_code).toBe("gst_free");

  const capital = toCreatePayload(
    values({ direction: "AP", gst_amount: "10.00", total: "110.00", tax_code: "capital" }),
    { gst_registered: true },
  );
  expect(capital.lines?.[0].tax_code).toBe("capital");

  const nonGst = toCreatePayload(
    values({ subtotal: "100.00", gst_amount: "10.00", total: "110.00", tax_code: "capital" }),
    { gst_registered: false },
  );
  expect(nonGst).toMatchObject({ subtotal: "110.00", gst_amount: "0", total: "110.00" });
  expect(nonGst.lines?.[0]).toMatchObject({
    line_subtotal: "110.00",
    line_gst: "0",
    line_total: "110.00",
    tax_code: "none",
  });
});

test("manual AP form exposes capital only for an Asset account and clears it on AR", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Invoice Tax UI Pty Ltd");
  const accountsResponse = await request.get(`${BACKEND_URL}/api/v1/accounts`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(accountsResponse.ok()).toBeTruthy();
  const accounts = (await accountsResponse.json()) as Array<{
    id: number;
    code: string;
  }>;
  const assetId = accounts.find((account) => account.code === "1700")?.id;
  expect(assetId).toBeTruthy();

  await page.goto("/invoices");
  await selectCompany(page);
  await page.getByRole("button", { name: "+ Manual", exact: true }).click();
  const dialog = page.getByRole("heading", { name: "New invoice" }).locator("../..");
  const accountSelect = dialog.getByLabel("Expense / asset account (needed to post to the ledger)");
  const taxSelect = dialog.getByLabel("GST treatment (Australian tax classification)");

  await expect(taxSelect).toHaveValue("gst_free");
  await dialog.getByLabel("Total (incl GST)").fill("110.00");
  await expect(taxSelect).toHaveValue("standard");
  await expect(accountSelect.locator(`option[value="${assetId}"]`)).toHaveCount(1);
  await expect(taxSelect.locator('option[value="capital"]')).toHaveCount(0);
  await accountSelect.selectOption(String(assetId));
  await expect(taxSelect.locator('option[value="capital"]')).toHaveCount(1);
  await taxSelect.selectOption("capital");
  await expect(taxSelect).toHaveValue("capital");

  await dialog.getByRole("button", { name: /^AR/ }).click();
  await expect(
    dialog.getByLabel("Income account (needed to post to the ledger)"),
  ).toHaveValue("");
  await expect(taxSelect.locator('option[value="capital"]')).toHaveCount(0);
  await expect(taxSelect).toHaveValue("standard");
});
