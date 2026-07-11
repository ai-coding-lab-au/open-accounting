import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  allocationsEqualTransactionAmount,
} from "../src/components/bank/InvoiceAllocationEditor";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
} from "./helpers";

const COMPANY_ID = "invoiceallocui";
const TXN_DATE = "2024-01-15";

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
  await expect(switcher).toHaveValue(COMPANY_ID);
}

async function accountId(
  request: APIRequestContext,
  code: string,
): Promise<number> {
  const response = await request.get(`${BACKEND_URL}/api/v1/accounts`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(response.ok()).toBeTruthy();
  const rows = (await response.json()) as Array<{ id: number; code: string }>;
  const account = rows.find((row) => row.code === code);
  expect(account, `Expected account ${code}`).toBeTruthy();
  return account!.id;
}

test("allocation totals use exact cents", () => {
  expect(
    allocationsEqualTransactionAmount(
      [
        { invoice_id: 1, amount: "0.10" },
        { invoice_id: 2, amount: "0.20" },
      ],
      "0.30",
    ),
  ).toBeTruthy();
  expect(
    allocationsEqualTransactionAmount(
      [{ invoice_id: 1, amount: "110.00" }],
      "110.01",
    ),
  ).toBeFalsy();
});

test("reconciliation requires and persists an explicit AR invoice allocation", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Invoice Allocation UI Pty Ltd");
  const headers = companyHeaders(COMPANY_ID);
  const incomeAccountId = await accountId(request, "4000");
  const arAccountId = await accountId(request, "1100");
  const customerDepositsId = await accountId(request, "2050");

  const invoiceNumber = "ALLOC-AR-001";
  const createdInvoice = await request.post(`${BACKEND_URL}/api/v1/invoices`, {
    headers,
    data: {
      direction: "AR",
      contact_name: "Allocation Customer",
      invoice_number: invoiceNumber,
      issue_date: "2024-01-01",
      due_date: "2024-01-31",
      currency: "AUD",
      subtotal: "100.00",
      gst_amount: "10.00",
      total: "110.00",
      gst_inclusive: true,
      source: "manual",
      lines: [
        {
          description: "Allocation test sale",
          account_id: incomeAccountId,
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

  const banksResponse = await request.get(`${BACKEND_URL}/api/v1/bank-accounts`, {
    headers,
  });
  expect(banksResponse.ok()).toBeTruthy();
  const bankId = ((await banksResponse.json()) as Array<{ id: number }>)[0].id;
  const memo = "Explicit AR allocation UI";
  const createdTransaction = await request.post(
    `${BACKEND_URL}/api/v1/bank-accounts/${bankId}/transactions`,
    {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data: {
        direction: "in",
        amount: "150.00",
        occurred_at: TXN_DATE,
        memo,
        account_id: null,
        gst_amount: "0.00",
        tax_code: "none",
        invoice_allocations: [],
      },
    },
  );
  expect(createdTransaction.ok(), await createdTransaction.text()).toBeTruthy();
  const transactionId = ((await createdTransaction.json()) as { id: number }).id;

  await page.goto("/reconciliation");
  await selectCompany(page);
  const transactionRow = page.getByRole("row").filter({ hasText: memo });
  await expect(transactionRow).toBeVisible();
  await transactionRow.getByRole("checkbox").last().check();
  await transactionRow.getByRole("combobox").first().selectOption(String(arAccountId));

  const editor = page.getByTestId("invoice-allocation-editor");
  await expect(editor).toBeVisible();
  const allocateButton = transactionRow.getByRole("button", { name: "Allocate" });
  await expect(allocateButton).toBeDisabled();
  await editor.getByLabel(`Allocate invoice ${invoiceNumber}`).check();
  await expect(editor.getByText("$40.00 unapplied", { exact: true })).toBeVisible();
  await expect(editor.getByLabel("Unapplied remainder account")).toHaveValue(
    String(customerDepositsId),
  );
  await expect(allocateButton).toBeEnabled();
  await allocateButton.click();
  await expect(transactionRow).toBeHidden();

  const transactions = await request.get(
    `${BACKEND_URL}/api/v1/bank-accounts/${bankId}/transactions`,
    { headers },
  );
  expect(transactions.ok()).toBeTruthy();
  const saved = (
    (await transactions.json()) as Array<{
      id: number;
      account_id: number | null;
      unapplied_account_id: number | null;
      unapplied_amount: string;
      invoice_allocations: Array<{
        invoice_id: number;
        amount: string;
        tax_components: Array<{
          tax_code: string;
          gross_amount: string;
          gst_amount: string;
        }>;
      }>;
    }>
  ).find((row) => row.id === transactionId);
  expect(saved).toMatchObject({
    account_id: arAccountId,
    unapplied_account_id: customerDepositsId,
    unapplied_amount: "40.00",
    invoice_allocations: [
      {
        invoice_id: invoiceId,
        amount: "110.00",
        tax_components: [
          { tax_code: "standard", gross_amount: "110.00", gst_amount: "10.00" },
        ],
      },
    ],
  });

  const invoiceResponse = await request.get(
    `${BACKEND_URL}/api/v1/invoices/${invoiceId}`,
    { headers },
  );
  expect(invoiceResponse.ok()).toBeTruthy();
  expect(await invoiceResponse.json()).toMatchObject({
    status: "paid",
    paid_amount: "110.00",
  });

  const secondInvoiceNumber = "ALLOC-AR-002";
  const secondInvoice = await request.post(`${BACKEND_URL}/api/v1/invoices`, {
    headers,
    data: {
      direction: "AR",
      contact_name: "Retry Customer",
      invoice_number: secondInvoiceNumber,
      issue_date: "2024-01-01",
      due_date: "2024-01-31",
      currency: "AUD",
      subtotal: "100.00",
      gst_amount: "10.00",
      total: "110.00",
      gst_inclusive: true,
      source: "manual",
      lines: [
        {
          description: "Manual retry allocation sale",
          account_id: incomeAccountId,
          line_subtotal: "100.00",
          line_gst: "10.00",
          line_total: "110.00",
        },
      ],
    },
  });
  expect(secondInvoice.ok(), await secondInvoice.text()).toBeTruthy();
  const secondInvoiceId = ((await secondInvoice.json()) as { id: number }).id;
  const secondPosted = await request.post(
    `${BACKEND_URL}/api/v1/invoices/${secondInvoiceId}/post`,
    { headers },
  );
  expect(secondPosted.ok(), await secondPosted.text()).toBeTruthy();

  await page.goto("/business-account");
  const newTransaction = page.getByRole("button", { name: "+ New transaction" });
  await newTransaction.click();
  const modal = page.getByRole("heading", { name: "New transaction" }).locator("../..");
  await modal.getByRole("button", { name: "Money in" }).click();
  await modal.getByLabel("Amount (AUD)").fill("110.00");
  const datePicker = modal.getByTitle("Pick a date");
  await datePicker.fill("2099-01-01");
  await modal
    .getByLabel("Show balance-sheet accounts (Assets / Liabilities / Equity)")
    .check();
  const category = modal
    .getByText("Category (drives P&L)")
    .locator("..")
    .getByRole("combobox");
  await expect(modal.getByText("Future transactions cannot use AR/AP invoice controls", { exact: false })).toBeVisible();
  await expect(category.locator(`option[value="${arAccountId}"]`)).toHaveCount(0);
  await datePicker.fill(TXN_DATE);
  await expect(category.locator(`option[value="${arAccountId}"]`)).toHaveCount(1);
  await category.selectOption(String(arAccountId));
  const manualEditor = modal.getByTestId("invoice-allocation-editor");
  await manualEditor.getByLabel(`Allocate invoice ${secondInvoiceNumber}`).check();
  const saveTransaction = modal.getByRole("button", { name: "Save transaction" });
  await expect(saveTransaction).toBeEnabled();

  const observedKeys: string[] = [];
  let abortFirstPost = true;
  await page.route("**/api/v1/bank-accounts/*/transactions", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    observedKeys.push(route.request().headers()["idempotency-key"] ?? "");
    if (abortFirstPost) {
      abortFirstPost = false;
      await route.abort("connectionreset");
      return;
    }
    await route.continue();
  });

  await saveTransaction.click();
  await expect.poll(() => observedKeys.length).toBe(1);
  await expect(saveTransaction).toBeEnabled();
  await saveTransaction.click();
  await expect(modal).toBeHidden();
  expect(observedKeys).toHaveLength(2);
  expect(observedKeys[0]).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  );
  expect(observedKeys[1]).toBe(observedKeys[0]);
});
