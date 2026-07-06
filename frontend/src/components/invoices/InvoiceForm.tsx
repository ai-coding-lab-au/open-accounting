import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import type { Account, InvoiceCreate, InvoiceDirection } from "../../types/api";
import DateInput from "../DateInput";

// Strip thousands separators so "1,000" parses/submits as 1000 instead of
// truncating to 1 (parseFloat) or 422-ing the backend Decimal parse.
function stripMoney(value: string): string {
  return (value ?? "").replace(/,/g, "").trim();
}

export interface InvoiceFormValues {
  direction: InvoiceDirection;
  contact_name: string;
  contact_abn: string;
  invoice_number: string;
  issue_date: string;
  due_date: string;
  subtotal: string;
  gst_amount: string;
  total: string;
  gst_inclusive: boolean;
  notes: string;
  // Single account code the whole invoice is coded to. Required for the
  // invoice to be authorised/posted to the ledger (a posting needs a coded
  // line). Income accounts for AR; expense/COGS for AP.
  account_id: number | "";
}

export const EMPTY_FORM: InvoiceFormValues = {
  direction: "AP",
  contact_name: "",
  contact_abn: "",
  invoice_number: "",
  issue_date: "",
  due_date: "",
  subtotal: "",
  gst_amount: "",
  total: "",
  gst_inclusive: true,
  notes: "",
  account_id: "",
};

export function toCreatePayload(
  v: InvoiceFormValues,
  opts: { source?: "manual" | "pdf" | "excel"; attachment_id?: string | null } = {}
): InvoiceCreate {
  const subtotal = stripMoney(v.subtotal) || "0";
  const gst = stripMoney(v.gst_amount) || "0";
  const total = stripMoney(v.total) || "0";
  // Code the whole invoice to one account so it can be posted to the ledger.
  // Without a coded line, authorising the invoice would 422. Only emit a line
  // when an account is chosen; otherwise leave it a header-only draft.
  const lines =
    v.account_id !== ""
      ? [
          {
            description: v.notes.trim() || `Invoice ${v.invoice_number.trim()}`,
            account_id: v.account_id,
            line_subtotal: subtotal,
            line_gst: gst,
            line_total: total,
          },
        ]
      : null;
  return {
    direction: v.direction,
    contact_name: v.contact_name.trim() || null,
    contact_abn: v.contact_abn.trim() || null,
    invoice_number: v.invoice_number.trim(),
    issue_date: v.issue_date,
    due_date: v.due_date || null,
    subtotal,
    gst_amount: gst,
    total,
    gst_inclusive: v.gst_inclusive,
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
  const set = <K extends keyof InvoiceFormValues>(k: K, v: InvoiceFormValues[K]) =>
    onChange({ ...value, [k]: v });

  // Keyed by company id like every other accounts query: per-company SQLite
  // ids collide across companies, so serving a stale cross-company list could
  // code an invoice line to the wrong account after a switch.
  const currentId = useCompanyStore((s) => s.currentId);
  const { data: accounts } = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: async () => (await api.get<Account[]>("/accounts")).data,
    enabled: !!currentId,
  });
  // AR is income; AP is expense / cost of sales. Only active accounts.
  const codeTypes = value.direction === "AR" ? ["INCOME"] : ["EXPENSE", "COST_OF_SALES"];
  const accountChoices = (accounts ?? [])
    .filter((a) => a.active && codeTypes.includes(a.type))
    .sort((a, b) => a.code.localeCompare(b.code));

  // Auto-derive missing money fields. If user enters total + subtotal, fill GST.
  // If user enters total only and GST is inclusive, derive subtotal/gst at 10%.
  const [autoDerive, setAutoDerive] = useState(true);
  useEffect(() => {
    if (!autoDerive) return;
    const sub = parseFloat(stripMoney(value.subtotal));
    const gst = parseFloat(stripMoney(value.gst_amount));
    const tot = parseFloat(stripMoney(value.total));
    if (!Number.isNaN(tot) && Number.isNaN(sub) && Number.isNaN(gst) && value.gst_inclusive) {
      const newSub = (tot / 1.1).toFixed(2);
      const newGst = (tot - parseFloat(newSub)).toFixed(2);
      onChange({ ...value, subtotal: newSub, gst_amount: newGst });
    } else if (!Number.isNaN(tot) && !Number.isNaN(sub) && Number.isNaN(gst)) {
      onChange({ ...value, gst_amount: (tot - sub).toFixed(2) });
    } else if (!Number.isNaN(sub) && !Number.isNaN(gst) && Number.isNaN(tot)) {
      onChange({ ...value, total: (sub + gst).toFixed(2) });
    }
    // We only auto-derive once per change of a single field, so it's intentional.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.total, value.subtotal, value.gst_amount, value.gst_inclusive]);

  return (
    <div className="space-y-3">
      {showDirection && (
        <Field label="Direction">
          <div className="flex gap-2">
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AP"
                  ? "bg-emerald-600 text-white border-emerald-600"
                  : "bg-surface text-slate-700 border-slate-300"
              }`}
              onClick={() => set("direction", "AP")}
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
              onClick={() => set("direction", "AR")}
            >
              AR · Invoice to customer
            </button>
          </div>
        </Field>
      )}

      <div className="grid grid-cols-2 gap-3">
        <Field label={value.direction === "AP" ? "Supplier name" : "Customer name"}>
          <input
            className="input"
            value={value.contact_name}
            onChange={(e) => set("contact_name", e.target.value)}
          />
        </Field>
        <Field label="ABN (optional)">
          <input
            className="input"
            value={value.contact_abn}
            onChange={(e) => set("contact_abn", e.target.value)}
          />
        </Field>
      </div>

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

      <div className="grid grid-cols-3 gap-3">
        <Field label="Subtotal (ex GST)">
          <input
            className="input"
            inputMode="decimal"
            value={value.subtotal}
            onChange={(e) => set("subtotal", e.target.value)}
          />
        </Field>
        <Field label="GST">
          <input
            className="input"
            inputMode="decimal"
            value={value.gst_amount}
            onChange={(e) => set("gst_amount", e.target.value)}
          />
        </Field>
        <Field label="Total (incl GST)">
          <input
            className="input"
            inputMode="decimal"
            value={value.total}
            onChange={(e) => set("total", e.target.value)}
          />
        </Field>
      </div>

      <Field
        label={value.direction === "AR" ? "Income account" : "Expense account"}
        hint="(needed to post to the ledger)"
      >
        <select
          className="input"
          value={value.account_id === "" ? "" : String(value.account_id)}
          onChange={(e) =>
            set("account_id", e.target.value === "" ? "" : Number(e.target.value))
          }
        >
          <option value="">— code later (stays a draft) —</option>
          {accountChoices.map((a) => (
            <option key={a.id} value={a.id}>
              {a.code} · {a.name}
            </option>
          ))}
        </select>
      </Field>

      <div className="flex items-center gap-4 text-xs text-slate-600">
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={value.gst_inclusive}
            onChange={(e) => set("gst_inclusive", e.target.checked)}
          />
          GST inclusive
        </label>
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={autoDerive}
            onChange={(e) => setAutoDerive(e.target.checked)}
          />
          Auto-derive missing money fields
        </label>
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
