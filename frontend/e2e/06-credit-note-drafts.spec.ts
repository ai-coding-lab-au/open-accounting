import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
} from "./helpers";

const COMPANY_ID = "creditnotedraftui";
const ISSUE_DATE = "2026-08-01";

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
  await expect(switcher).toHaveValue(COMPANY_ID);
}

async function createInvoice(
  request: APIRequestContext,
  invoiceNumber: string,
  accountId: number,
  lines: Array<{
    description: string;
    quantity: string;
    unit_price: string;
    line_subtotal: string;
    line_gst: string;
    line_total: string;
    tax_code: "standard" | "gst_free" | "none";
    gst_rate: string;
  }>,
  direction: "AR" | "AP" = "AR",
  contactName = "Fictional Credit Note Customer",
): Promise<number> {
  const subtotal = lines.reduce((sum, line) => sum + BigInt(line.line_subtotal.replace(".", "")), 0n);
  const gst = lines.reduce((sum, line) => sum + BigInt(line.line_gst.replace(".", "")), 0n);
  const asMoney = (cents: bigint) => `${cents / 100n}.${(cents % 100n).toString().padStart(2, "0")}`;
  const headers = companyHeaders(COMPANY_ID);
  const response = await request.post(`${BACKEND_URL}/api/v1/invoices`, {
    headers,
    data: {
      direction,
      contact_name: contactName,
      invoice_number: invoiceNumber,
      issue_date: ISSUE_DATE,
      currency: "AUD",
      subtotal: asMoney(subtotal),
      gst_amount: asMoney(gst),
      total: asMoney(subtotal + gst),
      gst_inclusive: false,
      source: "manual",
      lines: lines.map((line) => ({ ...line, account_id: accountId })),
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return ((await response.json()) as { id: number }).id;
}

async function postInvoice(request: APIRequestContext, invoiceId: number): Promise<void> {
  const response = await request.post(`${BACKEND_URL}/api/v1/invoices/${invoiceId}/post`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(response.ok(), await response.text()).toBeTruthy();
}

async function openInvoice(page: Page, invoiceNumber: string): Promise<void> {
  await page.getByRole("row").filter({ hasText: invoiceNumber }).click();
  await expect(page.getByRole("heading", { name: new RegExp(invoiceNumber) })).toBeVisible();
}

async function closeInvoice(page: Page): Promise<void> {
  await page.getByRole("button", { name: "×" }).click();
}

test("source-linked credit-note drafts stay isolated from invoice accounting", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Credit Note Draft Fictional Pty Ltd");
  const headers = companyHeaders(COMPANY_ID);
  const accountsResponse = await request.get(`${BACKEND_URL}/api/v1/accounts`, { headers });
  expect(accountsResponse.ok()).toBeTruthy();
  const accounts = (await accountsResponse.json()) as Array<{ id: number; code: string }>;
  const incomeAccount = accounts.find((account) => account.code === "4000");
  expect(incomeAccount).toBeTruthy();

  const draftInvoiceId = await createInvoice(request, "FICTIONAL-DRAFT-001", incomeAccount!.id, [
    {
      description: "Fictional draft service",
      quantity: "1",
      unit_price: "10.00",
      line_subtotal: "10.00",
      line_gst: "1.00",
      line_total: "11.00",
      tax_code: "standard",
      gst_rate: "0.10",
    },
  ]);
  const voidInvoiceId = await createInvoice(request, "FICTIONAL-VOID-001", incomeAccount!.id, [
    {
      description: "Fictional void service",
      quantity: "1",
      unit_price: "20.00",
      line_subtotal: "20.00",
      line_gst: "2.00",
      line_total: "22.00",
      tax_code: "standard",
      gst_rate: "0.10",
    },
  ]);
  await postInvoice(request, voidInvoiceId);
  const voidResponse = await request.post(`${BACKEND_URL}/api/v1/invoices/${voidInvoiceId}/void`, {
    headers,
  });
  expect(voidResponse.ok(), await voidResponse.text()).toBeTruthy();

  const sourceInvoiceId = await createInvoice(request, "FICTIONAL-SOURCE-001", incomeAccount!.id, [
    {
      description: "Fictional studio services",
      quantity: "2",
      unit_price: "100.00",
      line_subtotal: "200.00",
      line_gst: "20.00",
      line_total: "220.00",
      tax_code: "standard",
      gst_rate: "0.10",
    },
    {
      description: "Fictional setup services",
      quantity: "1.5",
      unit_price: "40.00",
      line_subtotal: "60.00",
      line_gst: "0.00",
      line_total: "60.00",
      tax_code: "gst_free",
      gst_rate: "0.00",
    },
  ]);
  await postInvoice(request, sourceInvoiceId);

  const noTaxInvoiceId = await createInvoice(request, "FICTIONAL-NO-TAX-001", incomeAccount!.id, [
    {
      description: "Fictional no-tax service",
      quantity: "1",
      unit_price: "35.00",
      line_subtotal: "35.00",
      line_gst: "0.00",
      line_total: "35.00",
      tax_code: "none",
      gst_rate: "0.00",
    },
  ]);
  await postInvoice(request, noTaxInvoiceId);

  const sourceBeforeResponse = await request.get(
    `${BACKEND_URL}/api/v1/invoices/${sourceInvoiceId}`,
    { headers },
  );
  expect(sourceBeforeResponse.ok()).toBeTruthy();
  const sourceBefore = await sourceBeforeResponse.json();
  const journalBeforeResponse = await request.get(`${BACKEND_URL}/api/v1/journal`, { headers });
  expect(journalBeforeResponse.ok()).toBeTruthy();
  const journalBefore = await journalBeforeResponse.json();

  const creditNoteRequests: Array<{ method: string; payload: unknown }> = [];
  page.on("request", (observed) => {
    if (!observed.url().includes("/api/v1/credit-notes") || !["POST", "PATCH"].includes(observed.method())) {
      return;
    }
    creditNoteRequests.push({
      method: observed.method(),
      payload: observed.postDataJSON(),
    });
  });

  await page.goto("/invoices");
  await selectCompany(page);
  await openInvoice(page, "FICTIONAL-DRAFT-001");
  await expect(page.getByRole("button", { name: "Create credit note" })).toHaveCount(0);
  await closeInvoice(page);
  await openInvoice(page, "FICTIONAL-VOID-001");
  await expect(page.getByRole("button", { name: "Create credit note" })).toHaveCount(0);
  await closeInvoice(page);

  await openInvoice(page, "FICTIONAL-NO-TAX-001");
  const noTaxSection = page.getByRole("region", { name: "Credit notes for source invoice" });
  await noTaxSection.getByRole("button", { name: "Create credit note" }).click();
  const noTaxDialog = page.getByRole("heading", { name: "Create draft credit note" }).locator("../..");
  await expect(noTaxDialog.getByText("No tax", { exact: true })).toBeVisible();
  await expect(noTaxDialog.getByText(/Tax code none/)).toBeVisible();
  await noTaxDialog.getByRole("button", { name: "Close" }).click();
  await closeInvoice(page);

  const sourceSnapshotResponsePromise = page.waitForResponse((response) =>
    response.url().includes(`/api/v1/credit-notes/source-invoices/${sourceInvoiceId}`) &&
    response.request().method() === "GET",
  );
  await openInvoice(page, "FICTIONAL-SOURCE-001");
  const sourceSnapshotResponse = await sourceSnapshotResponsePromise;
  expect(sourceSnapshotResponse.ok()).toBeTruthy();
  const sourceSnapshot = (await sourceSnapshotResponse.json()) as {
    lines: Array<{
      description: string;
      quantity_reserved: string;
      remaining_creditable_quantity: string;
    }>;
  };
  const sourceSection = page.getByRole("region", { name: "Credit notes for source invoice" });
  await expect(sourceSection.getByRole("button", { name: "Create credit note" })).toBeEnabled();
  await expect(sourceSection.getByText("Credit notes for this invoice")).toBeVisible();
  await sourceSection.getByRole("button", { name: "Create credit note" }).click();

  const createDialog = page.getByRole("heading", { name: "Create draft credit note" }).locator("../..");
  await expect(createDialog.getByText("FICTIONAL-SOURCE-001", { exact: true })).toBeVisible();
  for (const lockedValue of [
    "AR",
    "Fictional Credit Note Customer",
    "Source issue date",
    "Currency",
    "GST mode",
    "Source subtotal",
    "Source GST",
    "Source total",
    "Fictional studio services",
    "Fictional setup services",
    "Account ID",
    "Unit price",
    "GST rate",
    "Tax code",
    "Original source quantity",
    "Reserved by draft credit notes",
    "Remaining creditable quantity",
    "Source line subtotal",
    "Source line GST",
    "Source line total",
  ]) {
    await expect(createDialog.getByText(lockedValue, { exact: false }).first()).toBeVisible();
  }
  const sourceLine = sourceSnapshot.lines.find(
    (line) => line.description === "Fictional studio services",
  );
  expect(sourceLine).toBeTruthy();
  const sourceLineRow = createDialog.getByRole("row").filter({
    hasText: "Fictional studio services",
  });
  await expect(sourceLineRow.getByText(/Tax code standard/)).toBeVisible();
  const gstFreeLineRow = createDialog.getByRole("row").filter({
    hasText: "Fictional setup services",
  });
  await expect(gstFreeLineRow.getByText(/Tax code gst_free/)).toBeVisible();
  await expect(sourceLineRow.getByText(sourceLine!.quantity_reserved, { exact: true })).toBeVisible();
  await expect(sourceLineRow.getByText(sourceLine!.remaining_creditable_quantity, { exact: true })).toBeVisible();
  await expect(createDialog.getByRole("combobox")).toHaveCount(0);
  await expect(createDialog.getByLabel("Credit-note number")).toBeEnabled();
  await expect(createDialog.getByLabel("Issue date")).toBeEnabled();
  await expect(createDialog.getByLabel("Notes")).toBeEnabled();
  const firstQuantity = createDialog.getByLabel("Credited quantity for Fictional studio services");
  const secondQuantity = createDialog.getByLabel("Credited quantity for Fictional setup services");
  await expect(firstQuantity).toBeEnabled();
  await expect(secondQuantity).toBeEnabled();
  const editableInputLabels = await createDialog.locator("input:not([disabled])").evaluateAll((inputs) =>
    inputs.map((input) => input.getAttribute("aria-label")),
  );
  expect(editableInputLabels.sort()).toEqual([
    "Credited quantity for Fictional setup services",
    "Credited quantity for Fictional studio services",
    "Credit-note number",
    "Issue date",
  ].sort());

  for (const forbiddenControl of [/authorise/i, /post/i, /apply/i, /void/i, /refund/i, /journal/i, /payment/i, /balance/i]) {
    await expect(createDialog.getByRole("button", { name: forbiddenControl })).toHaveCount(0);
  }

  await createDialog.getByLabel("Credit-note number").fill("FICTIONAL-CN-001");
  await createDialog.getByLabel("Issue date").fill("2026-08-15");
  const createButton = createDialog.getByRole("button", { name: "Create draft" });
  await expect(createButton).toBeDisabled();
  await firstQuantity.fill("0");
  await expect(createButton).toBeDisabled();
  await firstQuantity.fill("0.00001");
  await expect(createButton).toBeDisabled();
  await firstQuantity.fill("3");
  await expect(createButton).toBeDisabled();
  await firstQuantity.fill("1.25");
  await expect(createButton).toBeEnabled();
  await createButton.click();

  await expect(createDialog.getByText("Subtotal $125.00", { exact: true })).toBeVisible();
  await expect(createDialog.getByText("GST $12.50", { exact: true })).toBeVisible();
  await expect(createDialog.getByText("Total $137.50", { exact: true })).toBeVisible();
  await expect(createDialog.getByText("Status draft", { exact: true })).toBeVisible();
  expect(creditNoteRequests[0]).toMatchObject({ method: "POST" });
  const createPayload = creditNoteRequests[0].payload as Record<string, unknown>;
  expect(Object.keys(createPayload).sort()).toEqual([
    "credit_note_number",
    "issue_date",
    "lines",
    "notes",
    "source_invoice_id",
  ]);
  expect(Object.keys((createPayload.lines as Array<Record<string, unknown>>)[0]).sort()).toEqual([
    "quantity",
    "source_invoice_line_id",
  ]);
  const createdDraftsResponse = await request.get(`${BACKEND_URL}/api/v1/credit-notes`, {
    headers,
    params: { source_invoice_id: sourceInvoiceId, status: "draft" },
  });
  expect(createdDraftsResponse.ok()).toBeTruthy();
  const createdDrafts = (await createdDraftsResponse.json()) as Array<{
    id: number;
    status: string;
    subtotal: string;
    gst_amount: string;
    total: string;
  }>;
  expect(createdDrafts).toHaveLength(1);
  expect(createdDrafts[0]).toMatchObject({
    status: "draft",
    subtotal: "125.00",
    gst_amount: "12.50",
    total: "137.50",
  });
  await createDialog.getByRole("button", { name: "Close" }).click();

  const sourceSectionAfterCreate = page.getByRole("region", { name: "Credit notes for source invoice" });
  const creditNoteRow = sourceSectionAfterCreate.getByRole("row").filter({ hasText: "FICTIONAL-CN-001" });
  await expect(creditNoteRow).toContainText("draft");
  await expect(creditNoteRow).toContainText("$125.00");
  await expect(creditNoteRow).toContainText("$12.50");
  await expect(creditNoteRow).toContainText("$137.50");
  const detailResponsePromise = page.waitForResponse((response) =>
    response.url().includes(`/api/v1/credit-notes/${createdDrafts[0].id}`) &&
    response.request().method() === "GET",
  );
  await creditNoteRow.getByRole("button", { name: "View/Edit" }).click();
  expect((await detailResponsePromise).ok()).toBeTruthy();

  const editDialog = page.getByRole("heading", { name: "Edit draft credit note" }).locator("../..");
  await expect(editDialog.getByLabel("Credit-note number")).toHaveValue("FICTIONAL-CN-001");
  await expect(editDialog.getByLabel("Issue date")).toHaveValue("2026-08-15");
  await expect(editDialog.getByText("Reserved by draft credit notes", { exact: true })).toBeVisible();
  await expect(editDialog.getByText("Remaining creditable quantity", { exact: true })).toBeVisible();
  await expect(editDialog.getByText("Maximum for this draft: 2", { exact: true })).toBeVisible();
  const editQuantity = editDialog.getByLabel("Credited quantity for Fictional studio services");
  const updateButton = editDialog.getByRole("button", { name: "Update draft" });
  await editQuantity.fill("2.0001");
  await expect(updateButton).toBeDisabled();
  await editQuantity.fill("2");
  await expect(updateButton).toBeEnabled();
  await editQuantity.fill("1.5");
  await editDialog.getByLabel("Credit-note number").fill("FICTIONAL-CN-001-EDIT");
  await updateButton.click();
  await expect(editDialog.getByText("Subtotal $150.00", { exact: true })).toBeVisible();
  await expect(editDialog.getByText("GST $15.00", { exact: true })).toBeVisible();
  await expect(editDialog.getByText("Total $165.00", { exact: true })).toBeVisible();

  expect(creditNoteRequests[1]).toMatchObject({ method: "PATCH" });
  const updatePayload = creditNoteRequests[1].payload as Record<string, unknown>;
  expect(Object.keys(updatePayload).sort()).toEqual([
    "credit_note_number",
    "issue_date",
    "lines",
    "notes",
  ]);
  expect(Object.keys((updatePayload.lines as Array<Record<string, unknown>>)[0]).sort()).toEqual([
    "quantity",
    "source_invoice_line_id",
  ]);
  await editDialog.getByRole("button", { name: "Close" }).click();

  const updatedRow = sourceSectionAfterCreate.getByRole("row").filter({ hasText: "FICTIONAL-CN-001-EDIT" });
  await expect(updatedRow).toContainText("$150.00");
  await expect(updatedRow).toContainText("$15.00");
  await expect(updatedRow).toContainText("$165.00");
  await updatedRow.getByRole("button", { name: "Delete draft" }).click();

  let confirmation = page.getByRole("heading", { name: "Delete this draft credit note?" }).locator("../..");
  await expect(confirmation).toContainText("Deleting removes only this draft");
  await expect(confirmation).toContainText("source invoice, journal, invoice balance, payment allocation, or GST report");
  await confirmation.getByRole("button", { name: "Cancel" }).click();

  await updatedRow.getByRole("button", { name: "Delete draft" }).click();
  confirmation = page.getByRole("heading", { name: "Delete this draft credit note?" }).locator("../..");
  await confirmation.getByRole("button", { name: "Delete draft" }).click();
  await expect(sourceSectionAfterCreate.getByRole("row").filter({ hasText: "FICTIONAL-CN-001-EDIT" })).toHaveCount(0);

  const sourceAfterResponse = await request.get(
    `${BACKEND_URL}/api/v1/invoices/${sourceInvoiceId}`,
    { headers },
  );
  expect(sourceAfterResponse.ok()).toBeTruthy();
  const sourceAfter = await sourceAfterResponse.json();
  expect(sourceAfter).toEqual(sourceBefore);
  expect(sourceAfter).toMatchObject({
    status: sourceBefore.status,
    subtotal: sourceBefore.subtotal,
    gst_amount: sourceBefore.gst_amount,
    total: sourceBefore.total,
    paid_amount: sourceBefore.paid_amount,
  });
  const journalAfterResponse = await request.get(`${BACKEND_URL}/api/v1/journal`, { headers });
  expect(journalAfterResponse.ok()).toBeTruthy();
  expect(await journalAfterResponse.json()).toEqual(journalBefore);

  const banksResponse = await request.get(`${BACKEND_URL}/api/v1/bank-accounts`, { headers });
  expect(banksResponse.ok()).toBeTruthy();
  const banks = (await banksResponse.json()) as Array<{ id: number }>;
  const transactions = await Promise.all(banks.map(async (bank) => {
    const response = await request.get(
      `${BACKEND_URL}/api/v1/bank-accounts/${bank.id}/transactions`,
      { headers },
    );
    expect(response.ok()).toBeTruthy();
    return response.json() as Promise<Array<{
      invoice_allocations: Array<{ invoice_id: number }>;
    }>>;
  }));
  expect(transactions.flat().some((transaction) =>
    transaction.invoice_allocations.some((allocation) => allocation.invoice_id === sourceInvoiceId),
  )).toBeFalsy();
});

test("AR and AP credit notes authorise to unapplied credits with immutable journals", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Credit Note Draft Fictional Pty Ltd");
  const headers = companyHeaders(COMPANY_ID);
  const accountsResponse = await request.get(`${BACKEND_URL}/api/v1/accounts`, { headers });
  expect(accountsResponse.ok()).toBeTruthy();
  const accounts = (await accountsResponse.json()) as Array<{ id: number; code: string }>;
  const scenarios = [
    {
      direction: "AR" as const,
      account: accounts.find((item) => item.code === "4000"),
      invoice: "FICTIONAL-AUTH-AR-SOURCE",
      creditNote: "FICTIONAL-AUTH-AR-CN",
      contact: "Fictional Authorisation Customer",
    },
    {
      direction: "AP" as const,
      account: accounts.find((item) => item.code === "6100"),
      invoice: "FICTIONAL-AUTH-AP-SOURCE",
      creditNote: "FICTIONAL-AUTH-AP-CN",
      contact: "Fictional Authorisation Supplier",
    },
  ];
  const sourceInvoiceIds = new Map<string, number>();
  for (const scenario of scenarios) {
    expect(scenario.account).toBeTruthy();
    const invoiceId = await createInvoice(
      request,
      scenario.invoice,
      scenario.account!.id,
      [{
        description: `Fictional ${scenario.direction} authorisation service`,
        quantity: "1",
        unit_price: "50.00",
        line_subtotal: "50.00",
        line_gst: "5.00",
        line_total: "55.00",
        tax_code: "standard",
        gst_rate: "0.10",
      }],
      scenario.direction,
      scenario.contact,
    );
    await postInvoice(request, invoiceId);
    sourceInvoiceIds.set(scenario.invoice, invoiceId);
  }

  await page.goto("/invoices");
  await selectCompany(page);

  for (const scenario of scenarios) {
    const invoiceId = sourceInvoiceIds.get(scenario.invoice)!;
    const invoiceBeforeResponse = await request.get(`${BACKEND_URL}/api/v1/invoices/${invoiceId}`, { headers });
    expect(invoiceBeforeResponse.ok()).toBeTruthy();
    const invoiceBefore = (await invoiceBeforeResponse.json()) as {
      status: string;
      paid_amount: string;
      subtotal: string;
      gst_amount: string;
      total: string;
    };
    await openInvoice(page, scenario.invoice);

    const sourceSection = page.getByRole("region", { name: "Credit notes for source invoice" });
    await sourceSection.getByRole("button", { name: "Create credit note" }).click();
    const createDialog = page.getByRole("heading", { name: "Create draft credit note" }).locator("../..");
    await createDialog.getByLabel("Credit-note number").fill(scenario.creditNote);
    await createDialog.getByLabel("Issue date").fill("2026-08-15");
    await createDialog.getByLabel(new RegExp("Credited quantity for Fictional")).fill("1");
    await createDialog.getByRole("button", { name: "Create draft" }).click();
    await expect(createDialog.getByText("Status draft", { exact: true })).toBeVisible();
    await createDialog.getByRole("button", { name: "Close" }).click();
    const createdNotesResponse = await request.get(`${BACKEND_URL}/api/v1/credit-notes`, {
      headers,
      params: { source_invoice_id: invoiceId, status: "draft" },
    });
    expect(createdNotesResponse.ok()).toBeTruthy();
    const createdNotes = (await createdNotesResponse.json()) as Array<{ id: number }>;
    expect(createdNotes).toHaveLength(1);

    const draftRow = sourceSection.getByRole("row").filter({ hasText: scenario.creditNote });
    await expect(draftRow).toContainText("draft");
    await expect(draftRow.getByRole("button", { name: "View/Edit" })).toBeVisible();
    await expect(draftRow.getByRole("button", { name: "Delete draft" })).toBeVisible();
    await draftRow.getByRole("button", { name: "View/Edit" }).click();

    const editDialog = page.getByRole("heading", { name: "Edit draft credit note" }).locator("../..");
    await editDialog.getByRole("button", { name: "Authorise" }).click();
    const confirmation = page.getByRole("heading", { name: "Authorise this credit note?" }).locator("../..");
    await expect(confirmation).toContainText("posts this credit note to the ledger");
    await expect(confirmation).toContainText("makes it immutable");
    await expect(confirmation).toContainText("leaves the credit available for approved invoice applications");
    await confirmation.getByRole("button", { name: "Cancel" }).click();
    await expect(editDialog.getByRole("button", { name: "Authorise" })).toBeVisible();

    await editDialog.getByRole("button", { name: "Authorise" }).click();
    const confirmationAgain = page.getByRole("heading", { name: "Authorise this credit note?" }).locator("../..");
    const postPromise = page.waitForResponse((response) =>
      response.url().includes(`/api/v1/credit-notes/${createdNotes[0].id}/post`) &&
      response.url().endsWith("/post") &&
      response.request().method() === "POST",
    );
    await confirmationAgain.getByRole("button", { name: "Authorise credit note" }).click();
    const postResponse = await postPromise;
    expect(postResponse.ok(), await postResponse.text()).toBeTruthy();
    expect(((await postResponse.json()) as { status: string }).status).toBe("authorised");
    const journalResponse = await request.get(`${BACKEND_URL}/api/v1/journal`, {
      headers,
      params: { source_type: scenario.direction === "AR" ? "credit_note_ar" : "credit_note_ap" },
    });
    expect(journalResponse.ok()).toBeTruthy();
    const journalEntries = (await journalResponse.json()) as Array<{
      source_id: number | null;
      memo: string;
      lines: unknown[];
    }>;
    expect(journalEntries.find((entry) => entry.source_id === createdNotes[0].id)).toMatchObject({
      memo: `Credit note ${scenario.creditNote}`,
      lines: expect.any(Array),
    });

    const authorisedDialog = page.getByRole("heading", { name: "View authorised credit note" }).locator("../..");
    await expect(authorisedDialog.getByText("Status authorised", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByText("Credit balances", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByText(/Applied credit/)).toBeVisible();
    await expect(authorisedDialog.getByText("Applied credit $0.00 · Remaining credit $55.00.")).toBeVisible();
    await expect(authorisedDialog.getByText(/Original journal entry #\d+/)).toBeVisible();
    await expect(authorisedDialog.getByRole("columnheader", { name: "Debit" })).toBeVisible();
    await expect(authorisedDialog.getByRole("columnheader", { name: "Credit", exact: true })).toBeVisible();
    await expect(authorisedDialog.getByLabel("Credit-note number")).toHaveCount(0);
    await expect(authorisedDialog.getByLabel("Issue date")).toHaveCount(0);
    await expect(authorisedDialog.getByLabel(/Credited quantity/)).toHaveCount(0);
    await expect(authorisedDialog.getByRole("button", { name: "Apply", exact: true })).toBeVisible();
    await expect(authorisedDialog.getByRole("button", { name: /refund/i })).toBeVisible();
    await expect(authorisedDialog.getByRole("button", { name: "Void", exact: true })).toBeVisible();
    for (const forbidden of [/authorise/i, /payment/i, /delete/i]) {
      await expect(authorisedDialog.getByRole("button", { name: forbidden })).toHaveCount(0);
    }

    const voidDate = "2026-08-20";
    await authorisedDialog.getByLabel("Void date").fill(voidDate);
    await authorisedDialog.getByRole("button", { name: "Void", exact: true }).click();
    const voidConfirmation = page.getByRole("heading", { name: "Void this credit note?" }).locator("..");
    await expect(voidConfirmation).toContainText("original ledger posting will be reversed");
    await expect(voidConfirmation).toContainText("Restoration is unavailable");
    const voidResponsePromise = page.waitForResponse((response) =>
      response.url().includes(`/api/v1/credit-notes/${createdNotes[0].id}/void`) &&
      response.request().method() === "POST",
    );
    await voidConfirmation.getByRole("button", { name: "Void credit note" }).click();
    const voidResponse = await voidResponsePromise;
    expect(voidResponse.ok(), await voidResponse.text()).toBeTruthy();
    const voidPayload = voidResponse.request().postDataJSON() as { void_date: string };
    expect(voidPayload).toEqual({ void_date: voidDate });
    expect((await voidResponse.json()) as { status: string }).toMatchObject({ status: "void" });
    await expect(authorisedDialog.getByText("Status void", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByText("Reversal journal", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByText(/Reverses entry ID \d+/)).toBeVisible();
    await expect(authorisedDialog.getByText(/Source type credit_note_void_(ar|ap)/)).toBeVisible();
    await expect(authorisedDialog.getByRole("button", { name: "Apply", exact: true })).toHaveCount(0);
    await expect(authorisedDialog.getByRole("button", { name: /refund/i })).toHaveCount(0);
    await expect(authorisedDialog.getByRole("button", { name: "Void", exact: true })).toHaveCount(0);
    await expect(authorisedDialog.getByLabel("Void date")).toHaveCount(0);

    const originalJournalResponse = await request.get(`${BACKEND_URL}/api/v1/journal`, {
      headers,
      params: { source_type: scenario.direction === "AR" ? "credit_note_ar" : "credit_note_ap" },
    });
    expect(originalJournalResponse.ok()).toBeTruthy();
    const originalJournal = (await originalJournalResponse.json()) as Array<{
      source_id: number | null;
      reverses_entry_id: number | null;
      lines: unknown[];
    }>;
    const originalEntry = originalJournal.find((entry) => entry.source_id === createdNotes[0].id);
    expect(originalEntry).toMatchObject({ reverses_entry_id: null, lines: expect.any(Array) });
    const reversalJournalResponse = await request.get(`${BACKEND_URL}/api/v1/journal`, {
      headers,
      params: { source_type: scenario.direction === "AR" ? "credit_note_void_ar" : "credit_note_void_ap" },
    });
    expect(reversalJournalResponse.ok()).toBeTruthy();
    const reversalJournal = (await reversalJournalResponse.json()) as Array<{
      source_id: number | null;
      entry_date: string;
      reverses_entry_id: number | null;
      lines: unknown[];
    }>;
    const reversalEntry = reversalJournal.find((entry) => entry.source_id === createdNotes[0].id);
    expect(reversalEntry).toMatchObject({
      entry_date: voidDate,
      reverses_entry_id: originalEntry?.id,
      lines: expect.any(Array),
    });

    await authorisedDialog.getByRole("button", { name: "Close" }).click();
    const authorisedRow = sourceSection.getByRole("row").filter({ hasText: scenario.creditNote });
    await expect(authorisedRow).toContainText("void");
    await expect(authorisedRow.getByRole("button", { name: "View" })).toBeVisible();
    await expect(authorisedRow.getByRole("button", { name: "View/Edit" })).toHaveCount(0);
    await expect(authorisedRow.getByRole("button", { name: "Delete draft" })).toHaveCount(0);
    await authorisedRow.getByRole("button", { name: "View" }).click();
    await expect(authorisedDialog.getByText("Status void", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByText("Credit balances", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByText(/Original journal entry #\d+/)).toBeVisible();
    await expect(authorisedDialog.getByText("Reversal journal", { exact: true })).toBeVisible();
    await expect(authorisedDialog.getByRole("button", { name: "Apply", exact: true })).toHaveCount(0);
    await expect(authorisedDialog.getByRole("button", { name: /refund/i })).toHaveCount(0);
    await expect(authorisedDialog.getByRole("button", { name: /authorise|payment|void|delete/i })).toHaveCount(0);
    await authorisedDialog.getByRole("button", { name: "Close" }).click();
    await closeInvoice(page);

    const sourceAfter = await request.get(
      `${BACKEND_URL}/api/v1/credit-notes/source-invoices/${invoiceId}`,
      { headers },
    );
    expect(sourceAfter.ok()).toBeTruthy();
    const sourceSnapshot = (await sourceAfter.json()) as {
      lines: Array<{ quantity_reserved: string; remaining_creditable_quantity: string }>;
    };
    expect(sourceSnapshot.lines[0]).toMatchObject({
      quantity_reserved: "0",
      remaining_creditable_quantity: "1.0000",
    });
    const invoiceAfter = await request.get(`${BACKEND_URL}/api/v1/invoices/${invoiceId}`, { headers });
    expect(invoiceAfter.ok()).toBeTruthy();
    expect(await invoiceAfter.json()).toMatchObject({
      status: invoiceBefore.status,
      paid_amount: invoiceBefore.paid_amount,
      subtotal: invoiceBefore.subtotal,
      gst_amount: invoiceBefore.gst_amount,
      total: invoiceBefore.total,
    });
  }
});
