import { expect, test, type Page } from "@playwright/test";
import {
  calculateNeutralLineAmount,
  createInvoiceLine,
  createEmptyInvoiceForm,
  EMPTY_FORM,
  toCreatePayload,
  synchronizeInvoiceAmounts,
  updateInvoiceLine,
  validateInvoiceForm,
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

function values(overrides: Partial<InvoiceFormValues> = {}): InvoiceFormValues {
  const line = {
    ...createInvoiceLine(),
    id: "test-line-1",
    description: "Consulting",
    quantity: "1",
    unit_price: "100.00",
    account_id: 42,
    tax_code: "gst_free" as const,
  };
  return {
    ...createEmptyInvoiceForm(),
    contact_id: 42,
    contact_name: "Tax Test",
    invoice_number: "TAX-1",
    issue_date: "2024-07-01",
    subtotal: "100.00",
    gst_amount: "0.00",
    total: "100.00",
    lines: [line],
    ...overrides,
  };
}

test("fresh forms and immutable line updates keep stable independent IDs", () => {
  const firstForm = createEmptyInvoiceForm();
  const secondForm = createEmptyInvoiceForm();
  expect(firstForm.lines).not.toBe(secondForm.lines);
  expect(firstForm.lines[0]).not.toBe(secondForm.lines[0]);
  expect(firstForm.lines[0].id).not.toBe(secondForm.lines[0].id);
  expect(firstForm.contact_id).toBeNull();
  expect(Object.isFrozen(EMPTY_FORM.lines)).toBeTruthy();
  expect(Object.isFrozen(EMPTY_FORM.lines[0])).toBeTruthy();
  const sentinelUpdate = updateInvoiceLine(EMPTY_FORM.lines, EMPTY_FORM.lines[0].id, {
    description: "Changed copy",
  });
  expect(EMPTY_FORM.lines[0].description).toBe("");
  expect(sentinelUpdate[0]).not.toBe(EMPTY_FORM.lines[0]);

  const first = { ...createInvoiceLine(), id: "stable-first", description: "First" };
  const second = { ...createInvoiceLine(), id: "stable-second", description: "Second" };
  const original = [first, second];
  const changed = updateInvoiceLine(original, first.id, { description: "Updated first" });
  expect(changed).not.toBe(original);
  expect(changed[0]).not.toBe(first);
  expect(changed[1]).toBe(second);
  expect(changed.map((line) => line.id)).toEqual(["stable-first", "stable-second"]);
  expect(changed[0].description).toBe("Updated first");
  expect(changed[1]).toEqual(second);

  const added = [...changed, createInvoiceLine()];
  expect(new Set(added.map((line) => line.id)).size).toBe(3);
  expect(added.slice(0, 2)).toEqual(changed);
});

test("neutral quantity-price arithmetic rounds exact half cents with HALF_UP", () => {
  expect(calculateNeutralLineAmount("1.49", "0.01")).toBe("0.01");
  expect(calculateNeutralLineAmount("1.5", "0.01")).toBe("0.02");
  expect(calculateNeutralLineAmount("1.51", "0.01")).toBe("0.02");
  expect(calculateNeutralLineAmount("0.5", "0.01")).toBe("0.01");
  expect(calculateNeutralLineAmount("1.00001", "0.01")).toBeNull();
  expect(calculateNeutralLineAmount("1", "-0.01")).toBeNull();
});

test("stored headers stay empty until every visible line is complete", () => {
  const incomplete = values({
    subtotal: "100.00",
    gst_amount: "0.00",
    total: "100.00",
    lines: [{ ...values().lines[0], account_id: "" }],
  });
  const pending = synchronizeInvoiceAmounts(incomplete);
  expect(pending).toMatchObject({ subtotal: "", gst_amount: "", total: "" });
  expect(calculateNeutralLineAmount("1", "100.00")).toBe("100.00");

  const completed = synchronizeInvoiceAmounts(values({ gst_inclusive: false }));
  expect(completed).toMatchObject({
    subtotal: "100.00", gst_amount: "0.00", total: "100.00", gst_inclusive: false,
  });
});

test("validation rejects every incomplete or inconsistent Phase A draft", () => {
  const valid = values();
  expect(validateInvoiceForm(valid)).toEqual([]);
  expect(validateInvoiceForm(values({ contact_id: null, contact_name: "Typed but unlinked" })).join(" ")).toContain("Select a supplier.");
  expect(validateInvoiceForm(values({ direction: "AR", contact_id: null })).join(" ")).toContain("Select a customer.");
  expect(validateInvoiceForm(values({ invoice_number: " " })).join(" ")).toContain("Invoice number is required");
  expect(validateInvoiceForm(values({ issue_date: "2024-02-30" })).join(" ")).toContain("valid issue date");
  expect(validateInvoiceForm(values({ lines: [] })).join(" ")).toContain("At least one invoice line");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], description: " " }] })).join(" ")).toContain("description is required");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], quantity: "0" }] })).join(" ")).toContain("quantity must be greater than zero");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], quantity: "-1" }] })).join(" ")).toContain("quantity must be greater than zero");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], unit_price: "not money" }] })).join(" ")).toContain("valid unit price");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], unit_price: "-0.01" }] })).join(" ")).toContain("valid unit price");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], account_id: "" }] })).join(" ")).toContain("account is required");
  expect(validateInvoiceForm(values({ lines: [{ ...valid.lines[0], tax_code: "invalid" as "gst_free" }] })).join(" ")).toContain("tax rate is required");
  expect(validateInvoiceForm(values({ subtotal: "", gst_amount: "", total: "" })).join(" ")).toContain("amounts are inconsistent");
  expect(validateInvoiceForm(values({ total: "101.00" })).join(" ")).toContain("amounts are inconsistent");
});

test("explicit exclusive, inclusive, and GST-free examples calculate exact line amounts", () => {
  const standard = { ...values().lines[0], tax_code: "standard" as const };
  const exclusive = synchronizeInvoiceAmounts(values({
    amount_mode: "exclusive",
    lines: [{ ...standard, unit_price: "1000.00" }],
  }));
  expect(exclusive).toMatchObject({
    subtotal: "1000.00", gst_amount: "100.00", total: "1100.00",
  });
  expect(toCreatePayload(exclusive).lines?.[0]).toMatchObject({
    line_subtotal: "1000.00", line_gst: "100.00", line_total: "1100.00",
  });

  const inclusive = synchronizeInvoiceAmounts(values({
    amount_mode: "inclusive",
    lines: [{ ...standard, unit_price: "1100.00" }],
  }));
  expect(inclusive).toMatchObject({
    subtotal: "1000.00", gst_amount: "100.00", total: "1100.00",
  });
  expect(toCreatePayload(inclusive).lines?.[0]).toMatchObject({
    line_subtotal: "1000.00", line_gst: "100.00", line_total: "1100.00",
  });

  const gstFree = synchronizeInvoiceAmounts(values({
    amount_mode: "inclusive",
    lines: [{ ...values().lines[0], quantity: "2", unit_price: "55.00" }],
  }));
  expect(gstFree).toMatchObject({ subtotal: "110.00", gst_amount: "0.00", total: "110.00" });
});

test("tax-code matrix and integer HALF_UP calculations match cents", () => {
  for (const amount_mode of ["exclusive", "inclusive"] as const) {
    for (const tax_code of ["standard", "capital", "gst_free", "input_taxed", "none"] as const) {
      const taxable = tax_code === "standard" || tax_code === "capital";
      const unit_price = taxable && amount_mode === "inclusive" ? "110.00" : "100.00";
      const direction = tax_code === "capital" ? "AP" : "AR";
      const result = synchronizeInvoiceAmounts(values({
        direction,
        amount_mode,
        lines: [{ ...values().lines[0], tax_code, unit_price }],
      }));
      expect(result.gst_amount).toBe(taxable ? "10.00" : "0.00");
    }
  }

  const exclusiveHalfUp = synchronizeInvoiceAmounts(values({
    amount_mode: "exclusive",
    lines: [{ ...values().lines[0], tax_code: "standard", unit_price: "0.05" }],
  }));
  expect(exclusiveHalfUp).toMatchObject({ subtotal: "0.05", gst_amount: "0.01", total: "0.06" });

  expect(calculateNeutralLineAmount("0.5", "0.01")).toBe("0.01");
  expect(calculateNeutralLineAmount("1.2345", "0.01")).toBe("0.01");
  expect(calculateNeutralLineAmount("1.00001", "0.01")).toBeNull();
});

test("line amounts round before header summation and No tax forces none", () => {
  const line = { ...values().lines[0], tax_code: "standard" as const, unit_price: "0.05" };
  const rounded = synchronizeInvoiceAmounts(values({ lines: [
    { ...line, id: "round-first" },
    { ...line, id: "round-second" },
  ] }));
  expect(rounded).toMatchObject({ subtotal: "0.10", gst_amount: "0.02", total: "0.12" });

  const noTax = synchronizeInvoiceAmounts(values({
    amount_mode: "none",
    lines: [{ ...line, tax_code: "capital" }],
  }));
  expect(noTax.amount_mode).toBe("none");
  expect(noTax.lines[0].tax_code).toBe("none");
  expect(noTax).toMatchObject({ subtotal: "0.05", gst_amount: "0.00", total: "0.05" });
  expect(toCreatePayload(noTax)).toMatchObject({ amount_mode: "none", gst_inclusive: false });
});

test("payload contains every visible calculated line using decimal-dollar strings", () => {
  const first = {
    ...createInvoiceLine(), id: "payload-1", description: "Consulting",
    quantity: "2", unit_price: "25.00", account_id: 42, tax_code: "standard" as const,
  };
  const second = {
    ...createInvoiceLine(), id: "payload-2", description: "Materials",
    quantity: "3", unit_price: "10.00", account_id: 43, tax_code: "gst_free" as const,
  };
  const payload = toCreatePayload(values({
    direction: "AR",
    notes: "Separate header note",
    subtotal: "80.00",
    gst_amount: "0.00",
    total: "80.00",
    lines: [first, second],
  }), { gst_registered: true });

  expect(payload.direction).toBe("AR");
  expect(payload.contact_id).toBe(42);
  expect(payload).not.toHaveProperty("contact_name");
  expect(payload).not.toHaveProperty("contact_abn");
  expect(payload).not.toHaveProperty("status");
  expect(payload.notes).toBe("Separate header note");
  expect(payload).not.toHaveProperty("account_id");
  expect(payload.lines).toEqual([
    {
      description: "Consulting", quantity: "2", unit_price: "25.00", account_id: 42,
      line_subtotal: "50.00", line_gst: "5.00", line_total: "55.00", tax_code: "standard",
    },
    {
      description: "Materials", quantity: "3", unit_price: "10.00", account_id: 43,
      line_subtotal: "30.00", line_gst: "0.00", line_total: "30.00", tax_code: "gst_free",
    },
  ]);
  expect(payload.subtotal).toBe("80.00");
  expect(payload.gst_amount).toBe("5.00");
  expect(payload.total).toBe("85.00");

  const invalidVisibleLine = {
    ...createInvoiceLine(), id: "invalid-visible", description: "", unit_price: "",
  };
  const invalidPayload = toCreatePayload(values({
    subtotal: "",
    gst_amount: "",
    total: "",
    lines: [first, invalidVisibleLine],
  }));
  expect(invalidPayload.lines).toHaveLength(2);
  expect(invalidPayload.lines?.[1]).toMatchObject({
    description: "", unit_price: "0", line_subtotal: "0.00", line_gst: "0.00",
  });
});

test("non-GST companies submit none with zero GST and normalized line codes", () => {
  const payload = toCreatePayload(values({
    gst_inclusive: true,
    lines: [
      { ...values().lines[0], tax_code: "standard" },
      { ...values().lines[0], id: "second-non-gst", tax_code: "capital" },
    ],
  }), { gst_registered: false });
  expect(payload).toMatchObject({
    subtotal: "200.00", gst_amount: "0.00", total: "200.00",
    amount_mode: "none", gst_inclusive: false,
  });
  expect(payload.lines).toHaveLength(2);
  expect(payload.lines?.map((line) => line.tax_code)).toEqual(["none", "none"]);
  expect(payload.lines?.every((line) =>
    line.line_gst === "0.00" && line.line_subtotal === line.line_total,
  )).toBeTruthy();
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
    type: string;
  }>;
  const assetId = accounts.find((account) => account.code === "1700")?.id;
  expect(assetId).toBeTruthy();

  await page.goto("/invoices");
  await selectCompany(page);
  await page.getByRole("button", { name: "+ Manual", exact: true }).click();
  const dialog = page.getByRole("heading", { name: "New invoice" }).locator("../..");
  const accountSelect = dialog.getByLabel("Account").first();
  const taxSelect = dialog.getByLabel("Tax rate").first();
  const amountMode = dialog.getByLabel("Amounts are");
  await dialog.getByLabel("Description").first().fill("Test equipment");
  await dialog.getByLabel("Qty").first().fill("1");
  await dialog.getByLabel("Unit price").first().fill("100.00");

  await expect(amountMode).toHaveValue("exclusive");
  await expect(amountMode.locator("option")).toHaveText([
    "Tax exclusive", "Tax inclusive", "No tax",
  ]);
  await expect(taxSelect).toHaveValue("gst_free");
  await expect(accountSelect.locator(`option[value="${assetId}"]`)).toHaveCount(1);
  await expect(taxSelect.locator('option[value="capital"]')).toHaveCount(0);
  await accountSelect.selectOption(String(assetId));
  await expect(taxSelect.locator('option[value="capital"]')).toHaveCount(1);
  await expect(taxSelect.locator("option")).toHaveText([
    "Standard", "GST-free", "Input-taxed", "Capital purchase", "Outside GST",
  ]);
  expect(await taxSelect.locator("option").evaluateAll((options) =>
    options.map((option) => (option as HTMLOptionElement).value),
  )).toEqual(["standard", "gst_free", "input_taxed", "capital", "none"]);
  await taxSelect.selectOption("capital");
  await expect(taxSelect).toHaveValue("capital");

  await amountMode.selectOption("none");
  await expect(taxSelect).toHaveValue("none");
  await expect(dialog.getByLabel("GST total")).toHaveText("0.00");

  await dialog.getByRole("button", { name: /^AR/ }).click();
  await expect(accountSelect).toHaveValue("");
  await expect(taxSelect.locator('option[value="capital"]')).toHaveCount(0);
  await expect(taxSelect.locator("option")).toHaveText([
    "Standard", "GST-free", "Input-taxed", "Outside GST",
  ]);
  expect(await taxSelect.locator("option").evaluateAll((options) =>
    options.map((option) => (option as HTMLOptionElement).value),
  )).toEqual(["standard", "gst_free", "input_taxed", "none"]);
  await expect(taxSelect).toHaveValue("none");
});

test("manual AR editor keeps independent lines, protects the final line, and submits all visible values", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Invoice Line UI Pty Ltd");
  const accountsResponse = await request.get(`${BACKEND_URL}/api/v1/accounts`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(accountsResponse.ok()).toBeTruthy();
  const accounts = (await accountsResponse.json()) as Array<{
    id: number;
    code: string;
    type: string;
    active: boolean;
  }>;
  const incomeAccount = accounts.find((account) => account.type === "INCOME" && account.active);
  expect(incomeAccount).toBeTruthy();
  const contactName = `Fictional Line State Customer ${Date.now()}`;
  const contactResponse = await request.post(`${BACKEND_URL}/api/v1/contacts`, {
    headers: companyHeaders(COMPANY_ID),
    data: { name: contactName, kind: "customer" },
  });
  expect(contactResponse.ok(), await contactResponse.text()).toBeTruthy();
  const contact = (await contactResponse.json()) as { id: number };

  await page.goto("/invoices");
  await selectCompany(page);
  await page.getByRole("button", { name: "+ Manual", exact: true }).click();
  const dialog = page.getByRole("heading", { name: "New invoice" }).locator("../..");
  await dialog.getByRole("button", { name: /^AR/ }).click();
  await dialog.getByLabel("Search customer contacts").fill(contactName);
  await dialog.getByRole("option", { name: `Select ${contactName}` }).click();
  await dialog.getByLabel("Invoice #").fill(`AR-LINES-${Date.now()}`);
  await dialog.getByLabel(/Issue date/).fill("01/07/2026");

  await expect(dialog.getByLabel("Remove line").first()).toBeDisabled();
  const rows = dialog.locator("table tbody tr");
  const accountSelects = rows.getByLabel("Account");
  await dialog.getByLabel("Description").first().fill("Consulting");
  await dialog.getByLabel("Qty").first().fill("2");
  await dialog.getByLabel("Unit price").first().fill("25.00");
  await expect(dialog.getByRole("button", { name: "Save Draft" })).toBeDisabled();
  await accountSelects.first().selectOption(String(incomeAccount!.id));
  await dialog.getByRole("button", { name: "Add line" }).click();
  await expect(accountSelects).toHaveCount(2);

  const firstId = await rows.nth(0).getAttribute("data-line-id");
  const secondId = await rows.nth(1).getAttribute("data-line-id");
  expect(firstId).toBeTruthy();
  expect(secondId).toBeTruthy();
  expect(secondId).not.toBe(firstId);
  await dialog.getByLabel("Description").nth(1).fill("Materials");
  await dialog.getByLabel("Qty").nth(1).fill("3");
  await dialog.getByLabel("Unit price").nth(1).fill("10.00");
  await accountSelects.nth(1).selectOption(String(incomeAccount!.id));
  await expect(dialog.getByLabel("Description").first()).toHaveValue("Consulting");
  await expect(dialog.getByLabel("Unit price").first()).toHaveValue("25.00");
  await expect(dialog.getByLabel("Invoice total")).toHaveText("80.00");
  await expect(dialog.getByLabel("Invoice total")).toHaveJSProperty("tagName", "OUTPUT");

  await dialog.getByLabel("Remove line").nth(1).click();
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toHaveAttribute("data-line-id", firstId!);
  await dialog.getByRole("button", { name: "Add line" }).click();
  await expect(rows).toHaveCount(2);
  const replacementId = await rows.nth(1).getAttribute("data-line-id");
  expect(replacementId).not.toBe(firstId);
  await dialog.getByLabel("Description").nth(1).fill("Materials");
  await dialog.getByLabel("Qty").nth(1).fill("3");
  await dialog.getByLabel("Unit price").nth(1).fill("10.00");
  await accountSelects.nth(1).selectOption(String(incomeAccount!.id));

  const createRequest = page.waitForRequest((outgoing) =>
    outgoing.method() === "POST" && outgoing.url().includes("/api/v1/invoices"),
  );
  await dialog.getByRole("button", { name: "Save Draft" }).click();
  const outgoing = await createRequest;
  const payload = outgoing.postDataJSON();
  expect(payload.direction).toBe("AR");
  expect(payload.contact_id).toBe(contact.id);
  expect(payload).not.toHaveProperty("contact_name");
  expect(payload).not.toHaveProperty("contact_abn");
  expect(payload.lines).toHaveLength(2);
  expect(payload.lines[0]).toMatchObject({
    description: "Consulting", quantity: "2", unit_price: "25.00", account_id: incomeAccount!.id,
    line_subtotal: "50.00", line_gst: "0.00", line_total: "50.00", tax_code: "gst_free",
  });
  expect(payload.lines[1]).toMatchObject({
    description: "Materials", quantity: "3", unit_price: "10.00", account_id: incomeAccount!.id,
    line_subtotal: "30.00", line_gst: "0.00", line_total: "30.00", tax_code: "gst_free",
  });
  expect(payload.total).toBe("80.00");
  await expect(dialog).toHaveCount(0);
});

test("manual keyboard submission rejects invalid fields while Save Draft is disabled", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Invoice Validation UI Pty Ltd");
  await page.goto("/invoices");
  await selectCompany(page);
  await page.getByRole("button", { name: "+ Manual", exact: true }).click();
  const dialog = page.getByRole("heading", { name: "New invoice" }).locator("../..");
  await expect(dialog.getByRole("button", { name: "Save Draft" })).toBeDisabled();
  await dialog.getByLabel("Search supplier contacts").focus();
  await page.keyboard.press("Enter");
  await expect(dialog.getByRole("alert")).toContainText("Select a supplier.");
  await expect(dialog.getByRole("alert")).toContainText("Invoice number is required");
  await expect(dialog.getByRole("alert")).toContainText("valid issue date is required");
  await expect(dialog.getByRole("alert")).toContainText("description is required");
});
