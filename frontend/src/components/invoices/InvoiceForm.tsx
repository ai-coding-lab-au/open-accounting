import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import { useCurrentCompany } from "../../lib/useCurrentCompany";
import type {
  Account,
  InvoiceCreate,
  InvoiceAmountMode,
  InvoiceDirection,
  TaxCode,
} from "../../types/api";
import DateInput from "../DateInput";
import AccountingContactSelect from "./AccountingContactSelect";
import InvoiceLineTable from "./InvoiceLineTable";

// Strip thousands separators so "1,000" parses/submits as 1000 instead of
// truncating to 1 (parseFloat) or 422-ing the backend Decimal parse.
function stripMoney(value: string): string {
  return (value ?? "").replace(/,/g, "").trim();
}

const TAX_CODES: TaxCode[] = ["standard", "gst_free", "input_taxed", "capital", "none"];
const AMOUNT_MODES: InvoiceAmountMode[] = ["exclusive", "inclusive", "none"];
const QUANTITY_SCALE = 10000n;
const PRICE_SCALE = 100n;

export interface InvoiceLineFormValue {
  id: string;
  description: string;
  quantity: string;
  unit_price: string;
  account_id: number | "";
  tax_code: TaxCode;
}

let lineIdSequence = 0;

export function createInvoiceLine(): InvoiceLineFormValue {
  const id = globalThis.crypto?.randomUUID?.() ?? `invoice-line-${Date.now()}-${++lineIdSequence}`;
  return {
    id,
    description: "",
    quantity: "1",
    unit_price: "",
    account_id: "",
    tax_code: "gst_free",
  };
}

export function updateInvoiceLine(
  lines: InvoiceLineFormValue[],
  id: string,
  updates: Partial<Omit<InvoiceLineFormValue, "id">>,
): InvoiceLineFormValue[] {
  return lines.map((line) => line.id === id ? { ...line, ...updates } : line);
}

export interface InvoiceFormValues {
  direction: InvoiceDirection;
  contact_id: number | null;
  contact_name: string;
  contact_abn: string;
  invoice_number: string;
  issue_date: string;
  due_date: string;
  subtotal: string;
  gst_amount: string;
  total: string;
  gst_inclusive: boolean;
  amount_mode: InvoiceAmountMode;
  notes: string;
  lines: InvoiceLineFormValue[];
}

export function createEmptyInvoiceForm(direction: InvoiceDirection = "AP"): InvoiceFormValues {
  return {
    direction,
    contact_id: null,
    contact_name: "",
    contact_abn: "",
    invoice_number: "",
    issue_date: "",
    due_date: "",
    subtotal: "",
    gst_amount: "",
    total: "",
    gst_inclusive: true,
    amount_mode: "exclusive",
    notes: "",
    lines: [createInvoiceLine()],
  };
}

const EMPTY_FORM_LINE = Object.freeze(createInvoiceLine());
export const EMPTY_FORM = Object.freeze({
  direction: "AP" as const,
  contact_id: null,
  contact_name: "",
  contact_abn: "",
  invoice_number: "",
  issue_date: "",
  due_date: "",
  subtotal: "",
  gst_amount: "",
  total: "",
  gst_inclusive: true,
  amount_mode: "exclusive" as const,
  notes: "",
  lines: Object.freeze([EMPTY_FORM_LINE]) as unknown as InvoiceLineFormValue[],
}) as InvoiceFormValues;

export interface InvoiceLineAmounts {
  subtotal: string;
  gst: string;
  total: string;
}

interface InvoiceAmounts {
  subtotalCents: bigint;
  gstCents: bigint;
  totalCents: bigint;
  complete: boolean;
  lines: Map<string, InvoiceLineAmounts | null>;
}

function parseScaledInteger(value: string, decimalPlaces: number): bigint | null {
  const raw = value.trim();
  if (raw.includes(",") && !/^\d{1,3}(?:,\d{3})*(?:\.\d+)?$/.test(raw)) return null;
  const normalized = stripMoney(raw);
  const pattern = new RegExp(`^(\\d+)(?:\\.(\\d{1,${decimalPlaces}}))?$`);
  const match = pattern.exec(normalized);
  if (!match) return null;
  const scale = 10n ** BigInt(decimalPlaces);
  const fraction = BigInt((match[2] ?? "").padEnd(decimalPlaces, "0") || "0");
  return BigInt(match[1]) * scale + fraction;
}

function parseQuantityUnits(value: string): bigint | null {
  return parseScaledInteger(value, 4);
}

function parsePriceCents(value: string): bigint | null {
  return parseScaledInteger(value, 2);
}

function formatCents(cents: bigint): string {
  const dollars = cents / PRICE_SCALE;
  const remainder = String(cents % PRICE_SCALE).padStart(2, "0");
  return `${dollars}.${remainder}`;
}

function neutralLineCents(line: InvoiceLineFormValue): bigint | null {
  const quantityUnits = parseQuantityUnits(line.quantity);
  const priceCents = parsePriceCents(line.unit_price);
  if (quantityUnits === null || priceCents === null) return null;
  return (quantityUnits * priceCents + 5000n) / QUANTITY_SCALE;
}

function roundHalfUp(numerator: bigint, denominator: bigint): bigint {
  return (numerator + denominator / 2n) / denominator;
}

function calculateLineAmounts(
  line: InvoiceLineFormValue,
  amountMode: InvoiceAmountMode,
): { subtotalCents: bigint; gstCents: bigint; totalCents: bigint } | null {
  const extendedCents = neutralLineCents(line);
  if (extendedCents === null) return null;

  if (amountMode === "none" || !["standard", "capital"].includes(line.tax_code)) {
    return { subtotalCents: extendedCents, gstCents: 0n, totalCents: extendedCents };
  }

  if (amountMode === "exclusive") {
    const gstCents = roundHalfUp(extendedCents, 10n);
    return {
      subtotalCents: extendedCents,
      gstCents,
      totalCents: extendedCents + gstCents,
    };
  }

  const gstCents = roundHalfUp(extendedCents, 11n);
  return {
    subtotalCents: extendedCents - gstCents,
    gstCents,
    totalCents: extendedCents,
  };
}

export function calculateNeutralLineAmount(
  quantity: string,
  unitPrice: string,
): string | null {
  const cents = neutralLineCents({
    id: "calculation",
    description: "",
    quantity,
    unit_price: unitPrice,
    account_id: "",
    tax_code: "gst_free",
  });
  return cents === null ? null : formatCents(cents);
}

function calculateInvoiceAmounts(
  lines: InvoiceLineFormValue[],
  amountMode: InvoiceAmountMode,
): InvoiceAmounts {
  const lineAmounts = new Map<string, InvoiceLineAmounts | null>();
  let subtotalCents = 0n;
  let gstCents = 0n;
  let totalCents = 0n;
  let complete = lines.length > 0;
  for (const line of lines) {
    const amounts = calculateLineAmounts(line, amountMode);
    lineAmounts.set(line.id, amounts === null ? null : {
      subtotal: formatCents(amounts.subtotalCents),
      gst: formatCents(amounts.gstCents),
      total: formatCents(amounts.totalCents),
    });
    if (amounts === null) {
      complete = false;
      continue;
    }
    subtotalCents += amounts.subtotalCents;
    gstCents += amounts.gstCents;
    totalCents += amounts.totalCents;
  }
  return { subtotalCents, gstCents, totalCents, complete, lines: lineAmounts };
}

function structurallyComplete(line: InvoiceLineFormValue, direction: InvoiceDirection): boolean {
  const quantity = parseQuantityUnits(line.quantity);
  const price = parsePriceCents(line.unit_price);
  return line.description.trim() !== "" &&
    quantity !== null && quantity > 0n &&
    price !== null &&
    Number.isInteger(line.account_id) && Number(line.account_id) > 0 &&
    TAX_CODES.includes(line.tax_code) &&
    !(direction === "AR" && line.tax_code === "capital");
}

export function synchronizeInvoiceAmounts(
  value: InvoiceFormValues,
  gstRegistered = true,
): InvoiceFormValues {
  const amountMode = gstRegistered ? value.amount_mode : "none";
  const lines = amountMode === "none"
    ? value.lines.map((line) => ({ ...line, tax_code: "none" as TaxCode }))
    : value.lines;
  const amounts = calculateInvoiceAmounts(lines, amountMode);
  const complete = amounts.complete && lines.every((line) =>
    structurallyComplete(line, value.direction),
  );
  return {
    ...value,
    amount_mode: amountMode,
    lines,
    subtotal: complete ? formatCents(amounts.subtotalCents) : "",
    gst_amount: complete ? formatCents(amounts.gstCents) : "",
    total: complete ? formatCents(amounts.totalCents) : "",
  };
}

function validIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

export function validateInvoiceForm(
  value: InvoiceFormValues,
  gstRegistered = true,
): string[] {
  const errors: string[] = [];
  const amountMode = gstRegistered ? value.amount_mode : "none";
  if (!AMOUNT_MODES.includes(amountMode)) errors.push("Select a valid amount mode.");
  if (!value.contact_id) errors.push(value.direction === "AP" ? "Select a supplier." : "Select a customer.");
  if (!value.invoice_number.trim()) errors.push("Invoice number is required.");
  if (!validIsoDate(value.issue_date)) errors.push("A valid issue date is required.");
  if (!value.lines.length) errors.push("At least one invoice line is required.");

  for (const [index, line] of value.lines.entries()) {
    const label = `Line ${index + 1}`;
    if (!line.description.trim()) errors.push(`${label}: description is required.`);
    const quantity = parseQuantityUnits(line.quantity);
    if (quantity === null || quantity <= 0n) errors.push(`${label}: quantity must be greater than zero.`);
    const unitPrice = parsePriceCents(line.unit_price);
    if (unitPrice === null) errors.push(`${label}: enter a valid unit price.`);
    if (!Number.isInteger(line.account_id) || Number(line.account_id) < 1) errors.push(`${label}: account is required.`);
    if (!TAX_CODES.includes(line.tax_code)) errors.push(`${label}: tax rate is required.`);
    if (line.tax_code === "capital" && value.direction !== "AP") {
      errors.push(`${label}: capital tax treatment is only valid for AP invoices.`);
    }
    if (gstRegistered && amountMode === "none" && line.tax_code !== "none") {
      errors.push(`${label}: No tax mode requires the Outside GST tax code.`);
    }
  }

  const lines = amountMode === "none" && !gstRegistered
    ? value.lines.map((line) => ({ ...line, tax_code: "none" as TaxCode }))
    : value.lines;
  const amounts = calculateInvoiceAmounts(lines, amountMode);
  const complete = amounts.complete && lines.every((line) =>
    structurallyComplete(line, value.direction),
  );
  if (complete && (
    value.subtotal !== formatCents(amounts.subtotalCents) ||
    value.gst_amount !== formatCents(amounts.gstCents) ||
    value.total !== formatCents(amounts.totalCents)
  )) {
    errors.push("Invoice amounts are inconsistent with the invoice lines.");
  }
  return errors;
}

export function toCreatePayload(
  v: InvoiceFormValues,
  opts: {
    source?: "manual" | "pdf" | "excel";
    attachment_id?: string | null;
    gst_registered?: boolean;
  } = {}
): InvoiceCreate {
  const gstRegistered = opts.gst_registered !== false;
  const amountMode = gstRegistered ? v.amount_mode : "none";
  const linesForCalculation = amountMode === "none"
    ? v.lines.map((line) => ({ ...line, tax_code: "none" as TaxCode }))
    : v.lines;
  const amounts = calculateInvoiceAmounts(linesForCalculation, amountMode);
  const lines = v.lines.map((line) => {
    const lineAmounts = amounts.lines.get(line.id);
    return {
      description: line.description.trim(),
      quantity: stripMoney(line.quantity) || "0",
      unit_price: stripMoney(line.unit_price) || "0",
      account_id: line.account_id === "" ? null : line.account_id,
      line_subtotal: lineAmounts?.subtotal ?? "0.00",
      line_gst: lineAmounts?.gst ?? "0.00",
      line_total: lineAmounts?.total ?? "0.00",
      tax_code: amountMode === "none" ? "none" : line.tax_code,
    };
  });
  const subtotal = formatCents(amounts.subtotalCents);
  const gst = formatCents(amounts.gstCents);
  const total = formatCents(amounts.totalCents);
  return {
    direction: v.direction,
    contact_id: v.contact_id,
    invoice_number: v.invoice_number.trim(),
    issue_date: v.issue_date,
    due_date: v.due_date || null,
    subtotal,
    gst_amount: gst,
    total,
    gst_inclusive: amountMode === "inclusive",
    amount_mode: amountMode,
    notes: v.notes.trim() || null,
    source: opts.source ?? "manual",
    attachment_id: opts.attachment_id ?? null,
    lines,
  };
}

interface Props {
  value: InvoiceFormValues;
  onChange: (v: InvoiceFormValues) => void;
  showDirection?: boolean;
}

export default function InvoiceForm({ value, onChange, showDirection = true }: Props) {
  const companyQ = useCurrentCompany();
  const gstRegistered = companyQ.data?.gst_registered !== false;
  const formGstRegistered = companyQ.data?.gst_registered ?? true;
  const set = <K extends keyof InvoiceFormValues>(k: K, v: InvoiceFormValues[K]) =>
    onChange(synchronizeInvoiceAmounts({ ...value, [k]: v }, formGstRegistered));
  const setAmountMode = (amountMode: InvoiceAmountMode) =>
    onChange(synchronizeInvoiceAmounts({ ...value, amount_mode: amountMode }, formGstRegistered));
  const setDirection = (direction: InvoiceDirection) => {
    if (direction === value.direction) return;
    const lines = value.lines.map((line) => ({
      ...line,
      account_id: "" as const,
      tax_code: direction === "AR" && line.tax_code === "capital"
        ? "gst_free" as TaxCode
        : line.tax_code,
    }));
    onChange(synchronizeInvoiceAmounts({
      ...value,
      direction,
      contact_id: null,
      contact_name: "",
      contact_abn: "",
      lines,
    }, formGstRegistered));
  };
  const updateLine = (
    id: string,
    updates: Partial<Omit<InvoiceLineFormValue, "id">>,
  ) => onChange(synchronizeInvoiceAmounts({
    ...value,
    lines: updateInvoiceLine(value.lines, id, updates),
  }, formGstRegistered));
  const addLine = () => onChange(synchronizeInvoiceAmounts({
    ...value,
    lines: [...value.lines, createInvoiceLine()],
  }, formGstRegistered));
  const removeLine = (id: string) => {
    if (value.lines.length <= 1) return;
    onChange(synchronizeInvoiceAmounts({
      ...value,
      lines: value.lines.filter((line) => line.id !== id),
    }, formGstRegistered));
  };

  // Keyed by company id like every other accounts query: per-company SQLite
  // ids collide across companies, so serving a stale cross-company list could
  // code an invoice line to the wrong account after a switch.
  const currentId = useCompanyStore((s) => s.currentId);
  const { data: accounts } = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: async () => (await api.get<Account[]>("/accounts")).data,
    enabled: !!currentId,
  });
  // AR is income; AP may be an ordinary expense/COGS or an asset acquisition.
  const codeTypes =
    value.direction === "AR"
      ? ["INCOME"]
      : ["ASSET", "EXPENSE", "COST_OF_SALES"];
  const accountChoices = (accounts ?? [])
    .filter(
      (a) =>
        a.active &&
        codeTypes.includes(a.type) &&
        !(
          value.direction === "AP" &&
          ["1000", "1100", "1200"].includes(a.code)
        ),
    )
    .sort((a, b) => a.code.localeCompare(b.code));
  const calculationMode = formGstRegistered ? value.amount_mode : "none";
  const amounts = calculateInvoiceAmounts(value.lines, calculationMode);

  return (
    <div className="space-y-3">
      {showDirection && (
        <div className="block text-sm">
          <span id="invoice-direction-label" className="block text-slate-600 mb-1">
            Direction
          </span>
          <div role="group" aria-labelledby="invoice-direction-label" className="flex gap-2">
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AP"
                  ? "bg-emerald-600 text-white border-emerald-600"
                  : "bg-surface text-slate-700 border-slate-300"
              }`}
              onClick={() => setDirection("AP")}
            >
              AP · Bill from supplier
            </button>
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AR"
                  ? "bg-emerald-600 text-white border-emerald-600"
                  : "bg-surface text-slate-700 border-slate-300"
              }`}
              onClick={() => setDirection("AR")}
            >
              AR · Invoice to customer
            </button>
          </div>
        </div>
      )}

      <AccountingContactSelect
        direction={value.direction}
        contactId={value.contact_id}
        contactName={value.contact_name}
        contactAbn={value.contact_abn}
        onChange={(contact) => onChange({
          ...value,
          contact_id: contact?.id ?? null,
          contact_name: contact?.name ?? "",
          contact_abn: contact?.abn ?? "",
        })}
      />

      <div className="grid grid-cols-3 gap-3">
        <Field label="Invoice #">
          <input
            className="input"
            value={value.invoice_number}
            onChange={(e) => set("invoice_number", e.target.value)}
          />
        </Field>
        <Field label="Issue date" hint="(DD/MM/YYYY)">
          <DateInput
            value={value.issue_date}
            onChange={(v) => set("issue_date", v)}
          />
        </Field>
        <Field label="Due date (optional)" hint="(DD/MM/YYYY)">
          <DateInput
            value={value.due_date}
            onChange={(v) => set("due_date", v)}
          />
        </Field>
      </div>

      <Field label="Amounts are">
        <select
          className="input max-w-xs"
          aria-label="Amounts are"
          value={calculationMode}
          disabled={!gstRegistered}
          onChange={(event) => setAmountMode(event.target.value as InvoiceAmountMode)}
        >
          <option value="exclusive">Tax exclusive</option>
          <option value="inclusive">Tax inclusive</option>
          <option value="none">No tax</option>
        </select>
      </Field>

      <InvoiceLineTable
        lines={value.lines}
        amounts={amounts.lines}
        accounts={accountChoices}
        direction={value.direction}
        gstRegistered={gstRegistered}
        onChange={updateLine}
        onAdd={addLine}
        onRemove={removeLine}
      />

      <div className="grid grid-cols-3 gap-3 border-t border-slate-200 pt-3 text-right tabular-nums">
        <div>
          <div className="text-xs text-slate-500">Subtotal</div>
          <output aria-label="Subtotal" className="font-medium">{amounts.complete ? formatCents(amounts.subtotalCents) : "—"}</output>
        </div>
        <div>
          <div className="text-xs text-slate-500">GST</div>
          <output aria-label="GST total" className="font-medium">{amounts.complete ? formatCents(amounts.gstCents) : "—"}</output>
        </div>
        <div>
          <div className="text-xs text-slate-500">Total</div>
          <output aria-label="Invoice total" className="font-semibold">{amounts.complete ? formatCents(amounts.totalCents) : "—"}</output>
        </div>
      </div>

      <Field label="Notes">
        <textarea
          className="input min-h-[60px]"
          value={value.notes}
          onChange={(e) => set("notes", e.target.value)}
        />
      </Field>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="block text-slate-600 mb-1">
        {label} {hint && <span className="text-slate-400 font-normal">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
