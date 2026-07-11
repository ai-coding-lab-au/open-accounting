import axios from "axios";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
  getCompanyById,
} from "./helpers";
import { api } from "../src/lib/api";
import { useCompanyStore } from "../src/store/company";

const TXN_DATE = "2024-01-15";

async function selectCompany(page: Page, companyId: string): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${companyId}"]`)).toHaveCount(1);
  await switcher.selectOption(companyId);
  await expect(switcher).toHaveValue(companyId);
}

async function dispatchCompanyChange(page: Page, companyId: string): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${companyId}"]`)).toHaveCount(1);
  await switcher.evaluate((node, value) => {
    const select = node as HTMLSelectElement;
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }, companyId);
  await expect(switcher).toHaveValue(companyId);
}

async function seedUncategorisedTransaction(
  request: APIRequestContext,
  companyId: string,
  memo: string,
): Promise<{ id: number; bankAccountId: number }> {
  const headers = companyHeaders(companyId);
  const accountsResponse = await request.get(`${BACKEND_URL}/api/v1/bank-accounts`, {
    headers,
  });
  expect(accountsResponse.ok()).toBeTruthy();
  const bankAccounts = (await accountsResponse.json()) as Array<{ id: number }>;
  expect(bankAccounts.length).toBeGreaterThan(0);
  const bankAccountId = bankAccounts[0].id;

  const existingResponse = await request.get(
    `${BACKEND_URL}/api/v1/bank-accounts/${bankAccountId}/transactions`,
    { headers },
  );
  expect(existingResponse.ok()).toBeTruthy();
  const existing = (await existingResponse.json()) as Array<{
    id: number;
    memo: string | null;
  }>;
  const found = existing.find((row) => row.memo === memo);
  if (found) return { id: found.id, bankAccountId };

  const created = await request.post(
    `${BACKEND_URL}/api/v1/bank-accounts/${bankAccountId}/transactions`,
    {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data: {
        direction: "out",
        amount: "110.00",
        occurred_at: TXN_DATE,
        memo,
        counter_party_name: "Company-scope regression",
        account_id: null,
        gst_amount: "0.00",
        tax_code: "standard",
      },
    },
  );
  expect(created.ok()).toBeTruthy();
  return {
    id: ((await created.json()) as { id: number }).id,
    bankAccountId,
  };
}

async function fetchBankTransaction(
  request: APIRequestContext,
  companyId: string,
  bankAccountId: number,
  transactionId: number,
): Promise<{ id: number; account_id: number | null; gst_amount: string }> {
  const response = await request.get(
    `${BACKEND_URL}/api/v1/bank-accounts/${bankAccountId}/transactions`,
    { headers: companyHeaders(companyId) },
  );
  expect(response.ok()).toBeTruthy();
  const rows = (await response.json()) as Array<{
    id: number;
    account_id: number | null;
    gst_amount: string;
  }>;
  const row = rows.find((candidate) => candidate.id === transactionId);
  expect(row, `Expected transaction ${transactionId} in ${companyId}`).toBeTruthy();
  return row!;
}

async function accountIdForCode(
  request: APIRequestContext,
  companyId: string,
  code: string,
): Promise<number> {
  const response = await request.get(`${BACKEND_URL}/api/v1/accounts`, {
    headers: companyHeaders(companyId),
  });
  expect(response.ok()).toBeTruthy();
  const accounts = (await response.json()) as Array<{ id: number; code: string }>;
  const account = accounts.find((row) => row.code === code);
  expect(account, `Expected account ${code} in ${companyId}`).toBeTruthy();
  return account!.id;
}

async function seedDraftInvoice(
  request: APIRequestContext,
  companyId: string,
  invoiceNumber: string,
): Promise<number> {
  const headers = companyHeaders(companyId);
  const existingResponse = await request.get(`${BACKEND_URL}/api/v1/invoices`, {
    headers,
  });
  expect(existingResponse.ok()).toBeTruthy();
  const existing = (await existingResponse.json()) as Array<{
    id: number;
    invoice_number: string;
  }>;
  const found = existing.find((row) => row.invoice_number === invoiceNumber);
  if (found) return found.id;

  const created = await request.post(`${BACKEND_URL}/api/v1/invoices`, {
    headers,
    data: {
      direction: "AR",
      contact_name: `Contact ${invoiceNumber}`,
      invoice_number: invoiceNumber,
      issue_date: TXN_DATE,
      due_date: "2024-02-15",
      currency: "AUD",
      subtotal: "100.00",
      gst_amount: "10.00",
      total: "110.00",
      gst_inclusive: true,
      source: "manual",
    },
  });
  expect(created.ok()).toBeTruthy();
  return ((await created.json()) as { id: number }).id;
}

test("Reconciliation selection cannot cross a company switch", async ({
  page,
  request,
}) => {
  const companyA = "p0recona";
  const companyB = "p0reconb";
  const memoA = "P0 scope A reconciliation";
  const memoB = "P0 scope B reconciliation";

  await ensureCompanyById(request, companyA, "P0 Reconciliation A");
  await ensureCompanyById(request, companyB, "P0 Reconciliation B");
  const txnA = await seedUncategorisedTransaction(request, companyA, memoA);
  const txnB = await seedUncategorisedTransaction(request, companyB, memoB);
  expect(txnA.id).toBe(txnB.id);
  const rentAccountId = await accountIdForCode(request, companyA, "6100");

  await page.goto("/reconciliation");
  await selectCompany(page, companyA);
  const rowA = page.getByRole("row").filter({ hasText: memoA });
  await expect(rowA).toBeVisible();
  await rowA.getByRole("checkbox").first().check();
  await page.getByLabel("Apply account").selectOption(String(rentAccountId));
  await expect(page.getByText("1 selected")).toBeVisible();

  await selectCompany(page, companyB);
  const rowB = page.getByRole("row").filter({ hasText: memoB });
  await expect(rowB).toBeVisible();
  await expect(rowB.getByRole("checkbox").first()).not.toBeChecked();
  await expect(page.getByText("1 selected")).toHaveCount(0);
  await expect(page.getByLabel("Apply account")).toHaveValue("");
  await expect(page.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();

  const bRowsResponse = await request.get(
    `${BACKEND_URL}/api/v1/bank-accounts/transactions/uncategorised`,
    { headers: companyHeaders(companyB) },
  );
  expect(bRowsResponse.ok()).toBeTruthy();
  const bRows = (await bRowsResponse.json()) as Array<{
    id: number;
    account_id: number | null;
  }>;
  expect(bRows.find((row) => row.id === txnB.id)?.account_id).toBeNull();
});

test("offline company-A Apply is not queued into company B", async ({
  page,
  request,
  context,
}) => {
  const companyA = "p0offlinea";
  const companyB = "p0offlineb";
  const memoA = "P0 offline A reconciliation";
  const memoB = "P0 offline B reconciliation";

  await ensureCompanyById(request, companyA, "P0 Offline A");
  await ensureCompanyById(request, companyB, "P0 Offline B");
  const txnA = await seedUncategorisedTransaction(request, companyA, memoA);
  const txnB = await seedUncategorisedTransaction(request, companyB, memoB);
  expect(txnA.id).toBe(txnB.id);
  const rentAccountId = await accountIdForCode(request, companyA, "6100");

  await page.goto("/reconciliation");
  await selectCompany(page, companyA);
  const rowA = page.getByRole("row").filter({ hasText: memoA });
  await expect(rowA).toBeVisible();
  await rowA.getByRole("checkbox").first().check();
  await page.getByLabel("Apply account").selectOption(String(rentAccountId));

  const companyAttempts: string[] = [];
  page.on("request", (outbound) => {
    if (
      outbound.method() === "PATCH" &&
      /\/bank-accounts\/transactions\/\d+\/categorise$/.test(
        new URL(outbound.url()).pathname,
      )
    ) {
      companyAttempts.push(outbound.headers()["x-company-id"] ?? "");
    }
  });

  await context.setOffline(true);
  try {
    await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);
    const failedPatch = page.waitForEvent("requestfailed", {
      predicate: (outbound) =>
        outbound.method() === "PATCH" && outbound.url().includes("/categorise"),
    });

    await page.getByRole("button", { name: "Apply", exact: true }).click();
    const failedRequest = await failedPatch;
    expect(failedRequest.headers()["x-company-id"]).toBe(companyA);
    await expect(page.getByRole("button", { name: "Apply", exact: true })).toBeEnabled();
    expect(companyAttempts).toEqual([companyA]);

    const aWhileOffline = await fetchBankTransaction(
      request,
      companyA,
      txnA.bankAccountId,
      txnA.id,
    );
    const bWhileOffline = await fetchBankTransaction(
      request,
      companyB,
      txnB.bankAccountId,
      txnB.id,
    );
    expect(aWhileOffline.account_id).toBeNull();
    expect(bWhileOffline.account_id).toBeNull();

    await selectCompany(page, companyB);
    const companyBReload = page.waitForResponse(
      (response) =>
        response.ok() &&
        response.url().includes("/transactions/uncategorised") &&
        response.request().headers()["x-company-id"] === companyB,
    );
    await context.setOffline(false);
    await companyBReload;
    await expect(page.getByLabel("Select company")).toHaveValue(companyB);
    await expect(page.getByRole("row").filter({ hasText: memoB })).toBeVisible();
    expect(companyAttempts).toEqual([companyA]);

    await selectCompany(page, companyA);
    await expect(page.getByRole("row").filter({ hasText: memoA })).toBeVisible();
    expect(companyAttempts).toEqual([companyA]);
  } finally {
    await context.setOffline(false);
  }

  const aAfterReconnect = await fetchBankTransaction(
    request,
    companyA,
    txnA.bankAccountId,
    txnA.id,
  );
  const bAfterReconnect = await fetchBankTransaction(
    request,
    companyB,
    txnB.bankAccountId,
    txnB.id,
  );
  expect(aAfterReconnect.account_id).toBeNull();
  expect(bAfterReconnect.account_id).toBeNull();
});

test("explicit company mismatch is reported as an Axios cancellation", async () => {
  const previousSelection = useCompanyStore.getState();
  let adapterCalls = 0;
  let caught: unknown;

  useCompanyStore.setState({
    currentId: "company-b",
    currentGeneration: "generation-b",
  });
  try {
    await api.get("/__scope_probe__", {
      headers: {
        "X-Company-Id": "company-a",
        "X-Company-Generation": "generation-a",
      },
      adapter: async (config) => {
        adapterCalls += 1;
        return {
          config,
          data: null,
          headers: {},
          status: 200,
          statusText: "OK",
        };
      },
    });
  } catch (error) {
    caught = error;
  } finally {
    useCompanyStore.setState({
      currentId: previousSelection.currentId,
      currentGeneration: previousSelection.currentGeneration,
    });
  }

  expect(adapterCalls).toBe(0);
  expect(axios.isCancel(caught)).toBe(true);
  expect((caught as { code?: string }).code).toBe("ERR_CANCELED");
});

test("explicit stale generation is cancelled before Axios dispatch", async () => {
  const previousSelection = useCompanyStore.getState();
  let adapterCalls = 0;
  let caught: unknown;

  useCompanyStore.setState({
    currentId: "company-a",
    currentGeneration: "generation-a2",
  });
  try {
    await api.get("/__generation_probe__", {
      headers: {
        "X-Company-Id": "company-a",
        "X-Company-Generation": "generation-a1",
      },
      adapter: async (config) => {
        adapterCalls += 1;
        return {
          config,
          data: null,
          headers: {},
          status: 200,
          statusText: "OK",
        };
      },
    });
  } catch (error) {
    caught = error;
  } finally {
    useCompanyStore.setState({
      currentId: previousSelection.currentId,
      currentGeneration: previousSelection.currentGeneration,
    });
  }

  expect(adapterCalls).toBe(0);
  expect(axios.isCancel(caught)).toBe(true);
  expect((caught as { code?: string }).code).toBe("ERR_CANCELED");
});

test("legacy persisted company id is upgraded before workspace requests", async ({
  page,
  request,
}) => {
  const companyId = `p0legacy${Date.now().toString(36)}`;
  const company = await ensureCompanyById(request, companyId, "P0 Legacy Store");

  await page.addInitScript((id) => {
    localStorage.setItem(
      "accounting.currentCompany",
      JSON.stringify({ state: { currentId: id }, version: 0 }),
    );
  }, companyId);

  const dashboardRequest = page.waitForRequest((outbound) =>
    new URL(outbound.url()).pathname.endsWith("/dashboard/summary"),
  );
  await page.goto("/dashboard");
  await expect(page.getByLabel("Select company")).toHaveValue(companyId);
  await expect(page.getByRole("heading", { name: "Bank accounts" })).toBeVisible();

  const outbound = await dashboardRequest;
  expect(outbound.headers()["x-company-id"]).toBe(companyId);
  expect(outbound.headers()["x-company-generation"]).toBe(
    company.generation_id,
  );
});

test("company creation selects the new company across the workspace boundary", async ({
  page,
  request,
}) => {
  const baseCompany = "p0createbase";
  const newCompany = `p0new${Date.now().toString(36)}`;
  await ensureCompanyById(request, baseCompany, "P0 Create Base");

  await page.goto("/dashboard");
  await selectCompany(page, baseCompany);
  await page.getByRole("button", { name: "New Company", exact: true }).click();
  await page.getByPlaceholder("e.g. company_a").fill(newCompany);
  await page.getByPlaceholder("e.g. Company A Pty Ltd").fill("P0 Newly Created");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.getByLabel("Select company")).toHaveValue(newCompany, {
    timeout: 10_000,
  });
  await expect(page.getByText("Create company")).toBeHidden();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  const companiesResponse = await request.get(`${BACKEND_URL}/api/v1/companies`);
  expect(companiesResponse.ok()).toBeTruthy();
  const companies = (await companiesResponse.json()) as Array<{ id: string }>;
  expect(companies.some((company) => company.id === newCompany)).toBe(true);
});

test("deleting the active company leaves the switcher on a valid company", async ({
  page,
  request,
}) => {
  const companyA = "p0deletea";
  const companyB = "p0deleteb";
  await ensureCompanyById(request, companyA, "P0 Delete A");
  await ensureCompanyById(request, companyB, "P0 Delete B");

  await page.goto("/dashboard");
  await selectCompany(page, companyA);
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByLabel(`Type the company id (${companyA}) to confirm`).fill(companyA);
  await page.getByRole("button", { name: "Delete permanently", exact: true }).click();

  await expect(page.getByText("Delete company")).toBeHidden({ timeout: 10_000 });
  await expect(page.getByLabel("Select company")).not.toHaveValue(companyA);
  await expect(
    page.getByLabel("Select company").locator(`option[value="${companyA}"]`),
  ).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Bank accounts" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Create company" })).toHaveCount(0);

  await expect.poll(async () => {
    const response = await request.get(`${BACKEND_URL}/api/v1/companies`);
    if (!response.ok()) return true;
    const companies = (await response.json()) as Array<{ id: string }>;
    return companies.some((company) => company.id === companyA);
  }).toBe(false);

  const selectedCompany = await page.getByLabel("Select company").inputValue();
  expect(selectedCompany).not.toBe("");
  const remainingResponse = await request.get(`${BACKEND_URL}/api/v1/companies`);
  const remaining = (await remainingResponse.json()) as Array<{ id: string }>;
  expect(remaining.some((company) => company.id === selectedCompany)).toBe(true);
});

test("Staff draft is discarded when the selected company changes", async ({
  page,
  request,
}) => {
  const companyA = "p0staffa";
  const companyB = "p0staffb";
  const draftName = "A-only unsaved staff draft";

  await ensureCompanyById(request, companyA, "P0 Staff A");
  await ensureCompanyById(request, companyB, "P0 Staff B");

  await page.goto("/settings");
  await selectCompany(page, companyA);
  await page.getByRole("button", { name: "+ New staff", exact: true }).click();
  await page.getByLabel("Full name").fill(draftName);
  await expect(page.getByLabel("Full name")).toHaveValue(draftName);

  await selectCompany(page, companyB);
  await expect(page.getByLabel("Full name")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ New staff", exact: true })).toBeVisible();

  const staffResponse = await request.get(
    `${BACKEND_URL}/api/v1/staff?include_inactive=true`,
    { headers: companyHeaders(companyB) },
  );
  expect(staffResponse.ok()).toBeTruthy();
  expect((await staffResponse.json()) as unknown[]).toHaveLength(0);
});

test("Invoice drawer closes before an action can target the new company", async ({
  page,
  request,
}) => {
  const companyA = "p0invoicea";
  const companyB = "p0invoiceb";
  const invoiceA = "P0-A-001";
  const invoiceB = "P0-B-001";

  await ensureCompanyById(request, companyA, "P0 Invoice A");
  await ensureCompanyById(request, companyB, "P0 Invoice B");
  const invoiceIdA = await seedDraftInvoice(request, companyA, invoiceA);
  const invoiceIdB = await seedDraftInvoice(request, companyB, invoiceB);
  expect(invoiceIdA).toBe(invoiceIdB);

  await page.goto("/invoices");
  await selectCompany(page, companyA);
  const invoiceRowA = page.getByRole("row").filter({ hasText: invoiceA });
  await expect(invoiceRowA).toBeVisible();
  await invoiceRowA.click();
  await expect(
    page.getByRole("button", { name: "Authorise (post to ledger)" }),
  ).toBeVisible();

  // The full-screen drawer blocks pointer access to the top bar, but the
  // company selector remains keyboard/programmatically reachable because the
  // modal has no focus trap. Dispatch the same change event a keyboard choice
  // would produce and verify that the old company subtree is synchronously gone.
  await dispatchCompanyChange(page, companyB);
  await expect(
    page.getByRole("button", { name: "Authorise (post to ledger)" }),
  ).toHaveCount(0);
  await expect(page.getByRole("row").filter({ hasText: invoiceB })).toBeVisible();

  const invoicesResponse = await request.get(`${BACKEND_URL}/api/v1/invoices`, {
    headers: companyHeaders(companyB),
  });
  expect(invoicesResponse.ok()).toBeTruthy();
  const invoices = (await invoicesResponse.json()) as Array<{
    id: number;
    invoice_number: string;
    status: string;
  }>;
  expect(invoices.find((row) => row.id === invoiceIdB)).toMatchObject({
    invoice_number: invoiceB,
    status: "draft",
  });
});

test("a stale company generation cannot mutate a recreated same-id company", async ({
  page,
  request,
}) => {
  const suffix = Date.now().toString(36);
  const companyId = `p0gen${suffix}`;
  const invoiceA1 = `GEN-A1-${suffix}`;
  const invoiceA2 = `GEN-A2-${suffix}`;

  const companyA1 = await ensureCompanyById(request, companyId, "Generation A1");
  const invoiceIdA1 = await seedDraftInvoice(request, companyId, invoiceA1);

  await page.goto("/invoices");
  await selectCompany(page, companyId);
  await page.getByRole("row").filter({ hasText: invoiceA1 }).click();
  await expect(page.getByRole("button", { name: "Delete draft" })).toBeVisible();

  const secondPage = await page.context().newPage();
  await secondPage.goto("/dashboard");
  await selectCompany(secondPage, companyId);
  await secondPage.getByRole("button", { name: "Delete", exact: true }).click();
  await secondPage
    .getByLabel(`Type the company id (${companyId}) to confirm`)
    .fill(companyId);
  await secondPage
    .getByRole("button", { name: "Delete permanently", exact: true })
    .click();
  await expect(secondPage.getByText("Delete company")).toBeHidden({
    timeout: 10_000,
  });

  await secondPage.getByRole("button", { name: "New Company", exact: true }).click();
  await secondPage.getByPlaceholder("e.g. company_a").fill(companyId);
  await secondPage.getByPlaceholder("e.g. Company A Pty Ltd").fill("Generation A2");
  await secondPage.getByRole("button", { name: "Create", exact: true }).click();
  await expect(secondPage.getByLabel("Select company")).toHaveValue(companyId, {
    timeout: 10_000,
  });

  const companyA2 = await getCompanyById(request, companyId);
  expect(companyA2).not.toBeNull();
  expect(companyA2!.generation_id).not.toBe(companyA1.generation_id);
  const invoiceIdA2 = await seedDraftInvoice(request, companyId, invoiceA2);
  expect(invoiceIdA2).toBe(invoiceIdA1);

  // Tab 1 still owns the A1 generation. Its stale equal-ID action may remain
  // visible, but the backend must reject it before opening A2's books.db.
  await expect(page.getByRole("button", { name: "Delete draft" })).toBeVisible();
  const rejectedDelete = page.waitForResponse(
    (response) =>
      response.request().method() === "DELETE" &&
      new URL(response.url()).pathname.endsWith(`/invoices/${invoiceIdA1}`),
  );
  await page.getByRole("button", { name: "Delete draft" }).click();
  await page.getByRole("button", { name: "Delete draft" }).last().click();
  const rejection = await rejectedDelete;
  expect(rejection.status()).toBe(409);
  expect(await rejection.json()).toMatchObject({
    detail: { code: "COMPANY_GENERATION_MISMATCH" },
  });
  await expect(
    page.getByText("This company workspace is stale; reload the company list."),
  ).toBeVisible();

  const invoicesResponse = await request.get(`${BACKEND_URL}/api/v1/invoices`, {
    headers: companyHeaders(companyId),
  });
  expect(invoicesResponse.ok()).toBeTruthy();
  const invoices = (await invoicesResponse.json()) as Array<{
    id: number;
    invoice_number: string;
    status: string;
  }>;
  expect(invoices.find((invoice) => invoice.id === invoiceIdA2)).toMatchObject({
    invoice_number: invoiceA2,
    status: "draft",
  });

  await secondPage.close();
});
