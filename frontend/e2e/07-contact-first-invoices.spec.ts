import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { Buffer } from "node:buffer";
import {
  BACKEND_URL,
  companyHeaders,
  ensureCompanyById,
} from "./helpers";

const COMPANY_ID = "contactfirstui";
const ISSUE_DATE = "2026-08-01";

interface ContactRecord {
  id: number;
  name: string;
  kind: "customer" | "supplier" | "both";
  active: boolean;
}

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
  await expect(switcher).toHaveValue(COMPANY_ID);
}

async function createContact(
  request: APIRequestContext,
  name: string,
  kind: ContactRecord["kind"],
  active = true,
): Promise<ContactRecord> {
  const response = await request.post(`${BACKEND_URL}/api/v1/contacts`, {
    headers: companyHeaders(COMPANY_ID),
    data: {
      name,
      kind,
      active,
      abn: kind === "supplier" ? "99 123 456 789" : null,
      email: `${kind}.fictional@example.test`,
      phone: "0400 000 123",
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as ContactRecord;
}

async function openInvoice(page: Page, invoiceNumber: string): Promise<void> {
  await page.getByRole("row").filter({ hasText: invoiceNumber }).click();
  await expect(page.getByRole("heading", { name: new RegExp(invoiceNumber) })).toBeVisible();
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${BACKEND_URL}/api/v1${path}`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as T;
}

function moneyToCents(value: string): bigint {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value);
  if (!match) throw new Error(`Invalid decimal money value: ${value}`);
  return BigInt(match[1]) * 100n + BigInt((match[2] ?? "").padEnd(2, "0") || "0");
}

function fictionalPdfBuffer(): Buffer {
  const stream = "BT /F1 12 Tf 72 720 Td (Fictional invoice attachment) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${Buffer.byteLength(stream, "ascii")} >>\nstream\n${stream}\nendstream`,
  ];
  let content = "%PDF-1.4\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(content, "ascii"));
    content += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const crossReferenceOffset = Buffer.byteLength(content, "ascii");
  content += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) {
    content += `${String(offset).padStart(10, "0")} 00000 n \n`;
  }
  content += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${crossReferenceOffset}\n%%EOF\n`;
  return Buffer.from(content, "ascii");
}

test("PDF invoice confirmation requires a selected active accounting contact", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Fictional Contact-First Pty Ltd");
  const supplierName = `Fictional PDF Supplier ${Date.now()}`;
  await createContact(request, supplierName, "supplier");
  const accounts = await getJson<Array<{ id: number; code: string }>>(request, "/accounts");
  const expense = accounts.find((account) => account.code === "6100");
  expect(expense).toBeTruthy();

  await page.goto("/invoices");
  await selectCompany(page);
  await page.getByRole("button", { name: "Attach PDF", exact: true }).click();
  const pdfDialog = page.getByRole("heading", { name: "Attach PDF invoice" }).locator("../..");
  await pdfDialog.locator('input[type="file"]').setInputFiles({
    name: "fictional-contact-gate.pdf",
    mimeType: "application/pdf",
    buffer: fictionalPdfBuffer(),
  });
  const uploadResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/invoices/upload-pdf") &&
    response.request().method() === "POST",
  );
  await pdfDialog.getByRole("button", { name: "Upload PDF" }).click();
  expect((await uploadResponse).ok()).toBeTruthy();

  const confirm = pdfDialog.getByRole("button", { name: "Confirm & save" });
  await expect(pdfDialog.getByLabel("Invoice #")).toBeVisible();
  await expect(confirm).toBeDisabled();
  const search = pdfDialog.getByLabel("Search supplier contacts");
  const contactResults = pdfDialog.getByRole("listbox", {
    name: "Supplier contact results",
  });
  await search.fill("Fictional Unmatched PDF Contact 60429");
  await expect(contactResults).toBeVisible();
  await expect(contactResults.getByRole("option")).toHaveCount(0);
  await expect(confirm).toBeDisabled();

  await pdfDialog.getByLabel("Invoice #").fill(`FICTIONAL-PDF-${Date.now()}`);
  await pdfDialog.getByLabel(/Issue date/).fill("01/08/2026");
  await expect(pdfDialog.getByLabel(/Issue date/)).toHaveValue("01/08/2026");
  await pdfDialog.getByLabel("Description").first().fill("Fictional PDF office supplies");
  await pdfDialog.getByLabel("Qty").first().fill("2");
  await pdfDialog.getByLabel("Unit price").first().fill("100.00");
  await pdfDialog.getByLabel("Account").first().selectOption(String(expense!.id));
  await pdfDialog.getByLabel("Tax rate").first().selectOption("standard");
  await expect(pdfDialog.getByLabel("Invoice total")).toHaveText("220.00");
  await expect(confirm).toBeDisabled();

  await search.fill(supplierName);
  const supplierOption = contactResults.getByRole("option", { name: `Select ${supplierName}` });
  await expect(supplierOption).toBeVisible();
  await supplierOption.click();
  await expect(pdfDialog.getByLabel("Selected accounting contact")).toContainText(supplierName);
  await expect(confirm).toBeEnabled();

  const selectedContact = pdfDialog.getByLabel("Selected accounting contact");
  await selectedContact.getByRole("button", { name: "Clear" }).click();
  await expect(selectedContact).toHaveCount(0);
  await expect(confirm).toBeDisabled();
});

test("contact-first AP entry posts correctly and keeps draft credit notes isolated", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Fictional Contact-First Pty Ltd");
  const supplierName = `Fictional Active Supplier ${Date.now()}`;
  const customerName = `Fictional Active Customer ${Date.now()}`;
  const bothName = `Fictional Both Contact ${Date.now()}`;
  const inactiveSupplierName = `Fictional Inactive Supplier ${Date.now()}`;
  const inactiveCustomerName = `Fictional Inactive Customer ${Date.now()}`;
  const supplier = await createContact(request, supplierName, "supplier");
  const customer = await createContact(request, customerName, "customer");
  const both = await createContact(request, bothName, "both");
  await createContact(request, inactiveSupplierName, "supplier", false);
  await createContact(request, inactiveCustomerName, "customer", false);

  const headers = companyHeaders(COMPANY_ID);
  const accounts = await getJson<Array<{ id: number; code: string; name: string }>>(request, "/accounts");
  const expense = accounts.find((account) => account.code === "6100");
  const gstPaid = accounts.find((account) => account.code === "1200");
  const payable = accounts.find((account) => account.code === "2000");
  expect(expense).toBeTruthy();
  expect(gstPaid).toBeTruthy();
  expect(payable).toBeTruthy();
  const invoiceNumber = `FICTIONAL-AP-BILL-${Date.now()}`;
  const inlineSupplierName = `Fictional Inline AP Supplier ${Date.now()}`;

  await page.goto("/invoices");
  await selectCompany(page);
  await page.getByRole("button", { name: "+ Manual", exact: true }).click();
  const dialog = page.getByRole("heading", { name: "New invoice" }).locator("../..");
  await dialog.getByLabel("Invoice #").fill(invoiceNumber);
  await dialog.getByLabel(/Issue date/).fill("01/08/2026");
  await expect(dialog.getByLabel(/Issue date/)).toHaveValue("01/08/2026");
  await dialog.getByLabel("Description").first().fill("Fictional office supplies");
  await dialog.getByLabel("Qty").first().fill("2");
  await dialog.getByLabel("Unit price").first().fill("100.00");
  await dialog.getByLabel("Account").first().selectOption(String(expense!.id));
  await dialog.getByLabel("Tax rate").first().selectOption("standard");
  await expect(dialog.getByLabel("Invoice total")).toHaveText("220.00");
  const apSearch = dialog.getByLabel("Search supplier contacts");
  const apContactResults = dialog.getByRole("listbox", {
    name: "Supplier contact results",
  });
  await apSearch.focus();
  const apSupplierOption = apContactResults.getByRole("option", { name: `Select ${supplierName}` });
  const apBothOption = apContactResults.getByRole("option", { name: `Select ${bothName}` });
  await expect(apSupplierOption).toBeVisible();
  await expect(apBothOption).toBeVisible();
  await expect(apContactResults.getByRole("option", { name: `Select ${customerName}` })).toHaveCount(0);
  await expect(apContactResults.getByRole("option", { name: new RegExp(inactiveSupplierName) })).toHaveCount(0);
  await expect(apSupplierOption).toContainText("supplier");
  await expect(apSupplierOption).toContainText("ABN 99123456789");
  await expect(apSupplierOption).toContainText("supplier.fictional@example.test");
  await expect(apSupplierOption).toContainText("0400000123");
  await apSearch.fill(bothName);
  await expect(apBothOption).toBeVisible();
  await expect(apSupplierOption).toHaveCount(0);
  await apSearch.fill("");
  await expect(apSupplierOption).toBeVisible();
  await apBothOption.click();
  await expect(dialog.getByLabel("Selected accounting contact")).toContainText(bothName);
  const saveDraft = dialog.getByRole("button", { name: "Save Draft" });
  await expect(saveDraft).toBeEnabled();
  const selectedContact = dialog.getByLabel("Selected accounting contact");
  await selectedContact.getByRole("button", { name: "Clear" }).click();
  await expect(selectedContact).toHaveCount(0);
  await expect(saveDraft).toBeDisabled();
  await apSearch.fill("Fictional No Matching Contact 82731");
  await expect(apContactResults).toBeVisible();
  await expect(apContactResults.getByRole("option")).toHaveCount(0);
  await expect(apContactResults.getByRole("option", { selected: true })).toHaveCount(0);
  await expect(saveDraft).toBeDisabled();
  await apSearch.fill("");
  await expect(apBothOption).toBeVisible();
  await apBothOption.click();
  await expect(dialog.getByLabel("Selected accounting contact")).toContainText(bothName);
  await expect(saveDraft).toBeEnabled();

  const directionGroup = dialog.getByRole("group", { name: "Direction" });
  await expect(directionGroup.getByRole("button", {
    name: "AP · Bill from supplier",
    exact: true,
  })).toBeVisible();
  await expect(directionGroup.getByRole("button", {
    name: "AR · Invoice to customer",
    exact: true,
  })).toBeVisible();
  await dialog.getByRole("button", { name: /^AR/ }).click();
  await expect(dialog.getByLabel("Selected accounting contact")).toHaveCount(0);
  const arSearch = dialog.getByLabel("Search customer contacts");
  const arContactResults = dialog.getByRole("listbox", {
    name: "Customer contact results",
  });
  await arSearch.focus();
  const arCustomerOption = arContactResults.getByRole("option", { name: `Select ${customerName}` });
  const arBothOption = arContactResults.getByRole("option", { name: `Select ${bothName}` });
  await expect(arCustomerOption).toBeVisible();
  await expect(arBothOption).toBeVisible();
  await expect(arContactResults.getByRole("option", { name: `Select ${supplierName}` })).toHaveCount(0);
  await expect(arContactResults.getByRole("option", { name: new RegExp(inactiveCustomerName) })).toHaveCount(0);
  await arCustomerOption.click();
  await expect(dialog.getByLabel("Selected accounting contact")).toContainText(customerName);

  await dialog.getByRole("button", { name: /^AP/ }).click();
  await expect(dialog.getByLabel("Selected accounting contact")).toHaveCount(0);
  const apContactResultsAfterSwitch = dialog.getByRole("listbox", {
    name: "Supplier contact results",
  });
  await dialog.getByLabel("Search supplier contacts").focus();
  await page.keyboard.press("Enter");
  await expect(dialog.getByRole("alert")).toContainText("Select a supplier.");

  await dialog.getByLabel("Account").first().selectOption(String(expense!.id));
  await dialog.getByLabel("Tax rate").first().selectOption("standard");
  await expect(dialog.getByLabel("Invoice #")).toHaveValue(invoiceNumber);
  await expect(dialog.getByLabel(/Issue date/)).toHaveValue("01/08/2026");
  await expect(dialog.getByLabel("Description").first()).toHaveValue("Fictional office supplies");
  await expect(dialog.getByLabel("Qty").first()).toHaveValue("2");
  await expect(dialog.getByLabel("Unit price").first()).toHaveValue("100.00");
  await expect(dialog.getByLabel("Invoice total")).toHaveText("220.00");
  await expect(saveDraft).toBeDisabled();
  await apSearch.fill(supplierName);
  const apSupplierOptionAfterSwitch = apContactResultsAfterSwitch.getByRole("option", {
    name: `Select ${supplierName}`,
  });
  await expect(apSupplierOptionAfterSwitch).toBeVisible();
  await apSupplierOptionAfterSwitch.click();
  await expect(dialog.getByLabel("Selected accounting contact")).toContainText(supplierName);
  await expect(saveDraft).toBeEnabled();

  await dialog.getByRole("button", { name: "+ New supplier" }).click();
  const newSupplierDialog = page.getByRole("dialog", { name: "New supplier" });
  await newSupplierDialog.getByLabel("Name").fill(inlineSupplierName);
  await newSupplierDialog.getByLabel("ABN").fill("99 987 654 321");
  await newSupplierDialog.getByLabel("Email").fill("inline.supplier@example.test");
  await newSupplierDialog.getByLabel("Phone").fill("0400 111 222");
  await newSupplierDialog.getByLabel("Address").fill("Fictional 10 Example Street");
  await newSupplierDialog.getByLabel("Notes").fill("Fictional inline supplier");
  const createContactResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/contacts") && response.request().method() === "POST",
  );
  await newSupplierDialog.getByRole("button", { name: "Create supplier" }).click();
  const inlineContact = (await (await createContactResponse).json()) as ContactRecord;
  expect(inlineContact.kind).toBe("supplier");
  await expect(newSupplierDialog).toHaveCount(0);
  await expect(dialog.getByLabel("Selected accounting contact")).toContainText(inlineSupplierName);
  await expect(dialog.getByLabel("Invoice #")).toHaveValue(invoiceNumber);
  await expect(dialog.getByLabel("Description").first()).toHaveValue("Fictional office supplies");
  await expect(dialog.getByLabel("Qty").first()).toHaveValue("2");
  await expect(dialog.getByLabel("Unit price").first()).toHaveValue("100.00");
  await expect(dialog.getByLabel("Invoice total")).toHaveText("220.00");

  const createInvoiceRequest = page.waitForRequest((observed) =>
    observed.url().includes("/api/v1/invoices") && observed.method() === "POST",
  );
  const createInvoiceResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/invoices") && response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Save Draft" }).click();
  const postedPayload = (await createInvoiceRequest).postDataJSON() as Record<string, unknown>;
  expect(postedPayload.direction).toBe("AP");
  expect(postedPayload.issue_date).toBe(ISSUE_DATE);
  expect(postedPayload.contact_id).toBe(inlineContact.id);
  expect(postedPayload).not.toHaveProperty("contact_name");
  expect(postedPayload).not.toHaveProperty("contact_abn");
  const draft = (await (await createInvoiceResponse).json()) as {
    id: number;
    direction: string;
    contact_id: number;
    status: string;
    paid_amount: string;
  };
  expect(draft).toMatchObject({
    direction: "AP",
    contact_id: inlineContact.id,
    status: "draft",
    paid_amount: "0.00",
  });

  await openInvoice(page, invoiceNumber);
  const authoriseResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/v1/invoices/${draft.id}/post`) &&
    response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Authorise (post to ledger)" }).click();
  const authorisation = (await (await authoriseResponse).json()) as {
    invoice: { direction: string; contact_id: number; status: string; paid_amount: string };
    journal_entry: { id: number; source_type: string; source_id: number };
  };
  expect(authorisation.invoice).toMatchObject({
    direction: "AP",
    contact_id: inlineContact.id,
    status: "authorised",
    paid_amount: "0.00",
  });
  expect(authorisation.journal_entry).toMatchObject({
    source_type: "invoice_ap",
    source_id: draft.id,
  });
  const originalJournal = await getJson<{
    id: number;
    source_type: string;
    source_id: number;
    lines: Array<{ account_id: number; debit_amount: string; credit_amount: string }>;
  }>(request, `/journal/${authorisation.journal_entry.id}`);
  expect(originalJournal.source_type).toBe("invoice_ap");
  const accountCodeById = new Map(accounts.map((account) => [account.id, account.code]));
  const amountFor = (code: string, side: "debit_amount" | "credit_amount") =>
    originalJournal.lines
      .filter((line) => accountCodeById.get(line.account_id) === code)
      .reduce((sum, line) => sum + moneyToCents(line[side]), 0n);
  expect(amountFor("6100", "debit_amount")).toBe(20000n);
  expect(amountFor("1200", "debit_amount")).toBe(2000n);
  expect(amountFor("2000", "credit_amount")).toBe(22000n);

  const sourceBefore = await getJson<Record<string, unknown>>(request, `/invoices/${draft.id}`);
  const journalBefore = await getJson<unknown[]>(request, "/journal");
  const sourceSnapshotResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/v1/credit-notes/source-invoices/${draft.id}`) &&
    response.request().method() === "GET",
  );
  await openInvoice(page, invoiceNumber);
  const sourceSection = page.getByRole("region", { name: "Credit notes for source invoice" });
  const createCreditNote = sourceSection.getByRole("button", { name: "Create credit note" });
  await expect(createCreditNote).toBeEnabled();
  await expect(sourceSection.getByText("Credit notes for this invoice")).toBeVisible();
  await createCreditNote.click();
  const sourceSnapshot = (await (await sourceSnapshotResponse).json()) as {
    direction: string;
    contact_name: string;
    lines: Array<{
      description: string;
      account_id: number;
      quantity: string;
      unit_price: string;
      tax_code: string;
      line_subtotal: string;
      line_gst: string;
      line_total: string;
    }>;
  };
  expect(sourceSnapshot.direction).toBe("AP");
  expect(sourceSnapshot.contact_name).toBe(inlineSupplierName);
  const createDialog = page.getByRole("heading", { name: "Create draft credit note" }).locator("../..");
  for (const lockedValue of [
    "AP",
    inlineSupplierName,
    "Fictional office supplies",
    "Account ID",
    `Account ID ${expense!.id}`,
    "Unit price $100.00",
    "Tax code standard",
    "Original source quantity 2",
    "Source line subtotal $200.00",
    "Source line GST $20.00",
    "Source line total $220.00",
  ]) {
    await expect(createDialog.getByText(lockedValue, { exact: false }).first()).toBeVisible();
  }
  const lockedSource = createDialog.getByRole("region", {
    name: "Locked source invoice details",
  });
  for (const [label, amount] of [
    ["Source subtotal", "$200.00"],
    ["Source GST", "$20.00"],
    ["Source total", "$220.00"],
  ] as const) {
    const row = lockedSource.getByText(label, { exact: true }).locator("..");
    await expect(row).toContainText(label);
    await expect(row).toContainText(amount);
  }
  const sourceLine = createDialog.getByRole("row").filter({ hasText: "Fictional office supplies" });
  await expect(sourceLine.getByText(/Tax code standard/)).toBeVisible();
  await expect(createDialog.getByRole("combobox")).toHaveCount(0);
  for (const forbiddenControl of [/authorise/i, /post/i, /apply/i, /refund/i, /payment/i, /balance/i]) {
    await expect(createDialog.getByRole("button", { name: forbiddenControl })).toHaveCount(0);
  }

  await createDialog.getByLabel("Credit-note number").fill("FICTIONAL-AP-CN-001");
  await createDialog.getByLabel("Issue date").fill("2026-08-15");
  const creditedQuantity = createDialog.getByLabel("Credited quantity for Fictional office supplies");
  await creditedQuantity.fill("1");
  const createDraftRequest = page.waitForRequest((observed) =>
    observed.url().includes("/api/v1/credit-notes") && observed.method() === "POST",
  );
  const createDraftButton = createDialog.getByRole("button", { name: "Create draft" });
  await expect(createDraftButton).toBeEnabled();
  await createDraftButton.click();
  const creditNotePayload = (await createDraftRequest).postDataJSON() as Record<string, unknown>;
  expect(creditNotePayload.source_invoice_id).toBe(draft.id);
  await expect(createDialog.getByText("Subtotal $100.00", { exact: true })).toBeVisible();
  await expect(createDialog.getByText("GST $10.00", { exact: true })).toBeVisible();
  await expect(createDialog.getByText("Total $110.00", { exact: true })).toBeVisible();
  await expect(createDialog.getByText("Status draft", { exact: true })).toBeVisible();
  await createDialog.getByRole("button", { name: "Close" }).click();

  const creditNoteRow = sourceSection.getByRole("row").filter({ hasText: "FICTIONAL-AP-CN-001" });
  await expect(creditNoteRow).toContainText("$110.00");
  await creditNoteRow.getByRole("button", { name: "View/Edit" }).click();
  const editDialog = page.getByRole("heading", { name: "Edit draft credit note" }).locator("../..");
  for (const forbiddenControl of [/authorise/i, /post/i, /apply/i, /refund/i, /payment/i, /balance/i]) {
    await expect(editDialog.getByRole("button", { name: forbiddenControl })).toHaveCount(0);
  }
  const editQuantity = editDialog.getByLabel("Credited quantity for Fictional office supplies");
  await editQuantity.fill("1.5");
  await editDialog.getByRole("button", { name: "Update draft" }).click();
  await expect(editDialog.getByText("Subtotal $150.00", { exact: true })).toBeVisible();
  await expect(editDialog.getByText("GST $15.00", { exact: true })).toBeVisible();
  await expect(editDialog.getByText("Total $165.00", { exact: true })).toBeVisible();
  await editDialog.getByRole("button", { name: "Close" }).click();
  const editedRow = sourceSection.getByRole("row").filter({ hasText: "FICTIONAL-AP-CN-001" });
  await editedRow.getByRole("button", { name: "Delete draft" }).click();
  const confirmation = page.getByRole("heading", { name: "Delete this draft credit note?" }).locator("../..");
  await confirmation.getByRole("button", { name: "Delete draft" }).click();
  await expect(sourceSection.getByRole("row").filter({ hasText: "FICTIONAL-AP-CN-001" })).toHaveCount(0);

  const sourceAfter = await getJson<Record<string, unknown>>(request, `/invoices/${draft.id}`);
  expect(sourceAfter).toEqual(sourceBefore);
  expect(sourceAfter.paid_amount).toBe(sourceBefore.paid_amount);
  const journalAfter = await getJson<unknown[]>(request, "/journal");
  expect(journalAfter).toEqual(journalBefore);
  const banks = await getJson<Array<{ id: number }>>(request, "/bank-accounts");
  const bankTransactions = await Promise.all(banks.map((bank) =>
    getJson<Array<{ invoice_allocations: Array<{ invoice_id: number }> }>>(
      request,
      `/bank-accounts/${bank.id}/transactions`,
    ),
  ));
  expect(bankTransactions.flat().some((transaction) =>
    transaction.invoice_allocations.some((allocation) => allocation.invoice_id === draft.id),
  )).toBeFalsy();

  await page.getByRole("button", { name: "×" }).click();
  await page.getByRole("button", { name: "+ Manual", exact: true }).click();
  const arInlineDialog = page.getByRole("heading", { name: "New invoice" }).locator("../..");
  await arInlineDialog.getByRole("button", { name: /^AR/ }).click();
  const arInvoiceNumber = `FICTIONAL-AR-INLINE-${Date.now()}`;
  const inlineCustomerName = `Fictional Inline AR Customer ${Date.now()}`;
  await arInlineDialog.getByLabel("Invoice #").fill(arInvoiceNumber);
  await arInlineDialog.getByRole("button", { name: "+ New customer" }).click();
  const newCustomerDialog = page.getByRole("dialog", { name: "New customer" });
  await newCustomerDialog.getByLabel("Name").fill(inlineCustomerName);
  const createCustomerResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/contacts") && response.request().method() === "POST",
  );
  await newCustomerDialog.getByRole("button", { name: "Create customer" }).click();
  const inlineCustomer = (await (await createCustomerResponse).json()) as ContactRecord;
  expect(inlineCustomer.kind).toBe("customer");
  await expect(arInlineDialog.getByLabel("Selected accounting contact")).toContainText(inlineCustomerName);
  await expect(arInlineDialog.getByLabel("Invoice #")).toHaveValue(arInvoiceNumber);
  await arInlineDialog.getByRole("button", { name: "Cancel" }).click();

  const createdContacts = await getJson<ContactRecord[]>(request, `/contacts?q=${encodeURIComponent(inlineSupplierName)}&active_only=true`);
  expect(createdContacts).toHaveLength(1);
  expect(createdContacts[0]).toMatchObject({ id: inlineContact.id, kind: "supplier", active: true });
  expect(supplier.active).toBeTruthy();
  expect(customer.active).toBeTruthy();
  expect(both.active).toBeTruthy();
  expect(gstPaid!.name).toContain("GST");
  expect(payable!.name).toContain("Payable");
});
