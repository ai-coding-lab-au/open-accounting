import { expect, test, type Page } from "@playwright/test";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
} from "./helpers";

const COMPANY_ID = "journalprovenanceui";

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
  await expect(switcher).toHaveValue(COMPANY_ID);
}

test("manual journals stay editable while invoice journals remain read-only", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Journal Provenance Fictional Pty Ltd");
  const headers = companyHeaders(COMPANY_ID);
  const accountsResponse = await request.get(`${BACKEND_URL}/api/v1/accounts`, {
    headers,
  });
  expect(accountsResponse.ok()).toBeTruthy();
  const accounts = (await accountsResponse.json()) as Array<{
    id: number;
    code: string;
  }>;
  const accountId = (code: string) => {
    const account = accounts.find((row) => row.code === code);
    expect(account, `Expected account ${code}`).toBeTruthy();
    return account!.id;
  };

  const manualMemo = "Fictional manual opening balance";
  const manual = await request.post(`${BACKEND_URL}/api/v1/journal`, {
    headers,
    data: {
      entry_date: "2026-05-31",
      memo: manualMemo,
      reference: "FICTIONAL-MANUAL-001",
      lines: [
        { account_id: accountId("1100"), debit_amount: "100.00" },
        { account_id: accountId("4000"), credit_amount: "100.00" },
      ],
    },
  });
  expect(manual.ok(), await manual.text()).toBeTruthy();
  const manualId = ((await manual.json()) as { id: number }).id;

  const invoiceNumber = "JOURNAL-PROVENANCE-001";
  const createdInvoice = await request.post(`${BACKEND_URL}/api/v1/invoices`, {
    headers,
    data: {
      direction: "AR",
      contact_name: "Journal Provenance Fictional Customer",
      invoice_number: invoiceNumber,
      issue_date: "2026-05-31",
      subtotal: "100.00",
      gst_amount: "10.00",
      total: "110.00",
      lines: [
        {
          description: "Fictional invoice service",
          account_id: accountId("4000"),
          quantity: "1",
          unit_price: "100.00",
          gst_rate: "0.10",
          line_subtotal: "100.00",
          line_gst: "10.00",
          line_total: "110.00",
        },
      ],
    },
  });
  expect(createdInvoice.ok(), await createdInvoice.text()).toBeTruthy();
  const invoiceId = ((await createdInvoice.json()) as { id: number }).id;

  const posted = await request.post(
    `${BACKEND_URL}/api/v1/invoices/${invoiceId}/post`,
    { headers },
  );
  expect(posted.ok(), await posted.text()).toBeTruthy();

  const voided = await request.post(
    `${BACKEND_URL}/api/v1/invoices/${invoiceId}/void`,
    { headers },
  );
  expect(voided.ok(), await voided.text()).toBeTruthy();

  await page.goto("/journal");
  await selectCompany(page);

  const manualRow = page.getByRole("row").filter({ hasText: manualMemo });
  await expect(manualRow.getByRole("button", { name: "Edit" })).toBeVisible();
  await expect(manualRow.getByRole("button", { name: "Delete" })).toBeVisible();
  await manualRow.getByRole("button", { name: "Edit" }).click();
  await expect(
    page.getByRole("heading", { name: `Edit journal entry #${manualId}` }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();

  const originalMemo = `Invoice ${invoiceNumber} — Journal Provenance Fictional Customer`;
  const reversalMemo = `Void invoice ${invoiceNumber}`;
  const originalRow = page.getByRole("row").filter({ hasText: originalMemo });
  const reversalRow = page.getByRole("row").filter({ hasText: reversalMemo });
  for (const row of [originalRow, reversalRow]) {
    await expect(row).toBeVisible();
    await expect(row.getByText(
      "System-generated journal. Correct through the source transaction.",
    )).toBeVisible();
    await expect(row.getByRole("button", { name: "Edit" })).toHaveCount(0);
    await expect(row.getByRole("button", { name: "Delete" })).toHaveCount(0);
    await row.getByText("show lines").click();
    await expect(row.getByText("Fictional invoice service")).toBeVisible();
  }

  await expect(page.getByRole("heading", { name: /^Edit journal entry/ })).toHaveCount(0);
});