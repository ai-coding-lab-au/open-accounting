import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import { useCurrentCompany } from "../../lib/useCurrentCompany";
import type {
  Account,
  InvoiceCreate,
  InvoiceDirection,
  TaxCode,
} from "../../types/api";
import DateInput from "../DateInput";

// Strip thousands separators so "1,000" parses/submits as 1000 instead of
// truncating to 1 (parseFloat) or 422-ing the backend Decimal parse.
function stripMoney(value: string): string {
  return (value ?? "").replace(/,/g, "").trim();
}

function defaultLineTaxCode(gstAmount: string): TaxCode {
  return Number(stripMoney(gstAmount) || 0) > 0 ? "standard" : "gst_free";
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
  tax_code: TaxCode;
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
  tax_code: "gst_free",
};

export function toCreatePayload(
  v: InvoiceFormValues,
  opts: {
    source?: "manual" | "pdf" | "excel";
    attachment_id?: string | null;
    gst_registered?: boolean;
  } = {}
): InvoiceCreate {
  let subtotal = stripMoney(v.subtotal) || "0";
  let gst = stripMoney(v.gst_amount) || "0";
  let total = stripMoney(v.total) || "0";
  if (opts.gst_registered === false) {
    // A non-registered company records the complete gross amount as income or
    // expense. Never send a synthetic GST split, even if stale form state was
    // populated before company metadata finished loading.
    const gross = total !== "0" ? total : subtotal;
    subtotal = gross;
    gst = "0";
    total = gross;
  }
  const taxCode: TaxCode =
    opts.gst_registered === false
      ? "none"
      : Number(gst || 0) > 0 &&
          ["gst_free", "input_taxed", "none"].includes(v.tax_code)
        ? "standard"
        : v.tax_code || defaultLineTaxCode(gst);
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
            tax_code: taxCode,
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
    gst_inclusive: opts.gst_registered === false ? false : v.gst_inclusive,
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
  const [taxCodeTouched, setTaxCodeTouched] = useState(false);
  const set = <K extends keyof InvoiceFormValues>(k: K, v: InvoiceFormValues[K]) =>
    onChange({ ...value, [k]: v });
  const setDirection = (direction: InvoiceDirection) => {
    if (direction === value.direction) return;
    // Account ids are not portable across invoice directions: AR requires
    // income, while AP requires expense/COGS.  Clear the hidden old selection
    // atomically with the direction switch so it cannot leak into submission.
    const resetCapital = direction === "AR" && value.tax_code === "capital";
    if (resetCapital) setTaxCodeTouched(false);
    onChange({
      ...value,
      direction,
      account_id: "",
      tax_code: resetCapital
        ? defaultLineTaxCode(value.gst_amount)
        : value.tax_code,
    });
  };

  // Keyed by company id like every other accounts query: per-company SQLite
  // ids collide across companies, so serving a stale cross-company list could
  // code an invoice line to the wrong account after a switch.
  const currentId = useCompanyStore((s) => s.currentId);
  const companyQ = useCurrentCompany();
  const gstRegistrationKnown = companyQ.data != null;
  const gstRegistered = companyQ.data?.gst_registered === true;
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
  const selectedAccount =
    value.account_id === ""
      ? null
      : accountChoices.find((account) => account.id === value.account_id) ?? null;
  const capitalEligible =
    value.direction === "AP" && selectedAccount?.type === "ASSET";

  // Auto-derive missing money fields. If user enters total + subtotal, fill GST.
  // If user enters total only and GST is inclusive, derive subtotal/gst at 10%.
  const [autoDerive, setAutoDerive] = useState(true);
  useEffect(() => {
    if (!gstRegistrationKnown) return;
    const sub = parseFloat(stripMoney(value.subtotal));
    const gst = parseFloat(stripMoney(value.gst_amount));
    const tot = parseFloat(stripMoney(value.total));
    const withTaxDefault = (
      updates: Partial<InvoiceFormValues>,
    ): InvoiceFormValues => {
      const merged = { ...value, ...updates };
      return {
        ...merged,
        tax_code: !gstRegistered
          ? "none"
          : taxCodeTouched
            ? merged.tax_code
            : defaultLineTaxCode(merged.gst_amount),
      };
    };
    if (!gstRegistered) {
      const gross = !Number.isNaN(tot) ? tot : !Number.isNaN(sub) ? sub : NaN;
      if (!Number.isNaN(gross)) {
        const amount = gross.toFixed(2);
        if (
          stripMoney(value.subtotal) !== amount ||
          stripMoney(value.gst_amount) !== "0" ||
          stripMoney(value.total) !== amount ||
          value.gst_inclusive ||
          value.tax_code !== "none"
        ) {
          onChange(withTaxDefault({
            subtotal: amount,
            gst_amount: "0",
            total: amount,
            gst_inclusive: false,
          }));
        }
      }
      return;
    }
    if (!autoDerive) {
      const desiredTax = taxCodeTouched
        ? value.tax_code
        : defaultLineTaxCode(value.gst_amount);
      if (desiredTax !== value.tax_code) {
        onChange({ ...value, tax_code: desiredTax });
      }
      return;
    }
    if (!Number.isNaN(tot) && Number.isNaN(sub) && Number.isNaN(gst) && value.gst_inclusive) {
      const newSub = (tot / 1.1).toFixed(2);
      const newGst = (tot - parseFloat(newSub)).toFixed(2);
      onChange(withTaxDefault({ subtotal: newSub, gst_amount: newGst }));
    } else if (!Number.isNaN(tot) && !Number.isNaN(sub) && Number.isNaN(gst)) {
      onChange(withTaxDefault({ gst_amount: (tot - sub).toFixed(2) }));
    } else if (!Number.isNaN(sub) && !Number.isNaN(gst) && Number.isNaN(tot)) {
      onChange(withTaxDefault({ total: (sub + gst).toFixed(2) }));
    } else {
      const desiredTax = taxCodeTouched
        ? value.tax_code
        : defaultLineTaxCode(value.gst_amount);
      if (desiredTax !== value.tax_code) {
        onChange({ ...value, tax_code: desiredTax });
      }
    }
    // We only auto-derive once per change of a single field, so it's intentional.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    value.total,
    value.subtotal,
    value.gst_amount,
    value.gst_inclusive,
    gstRegistered,
    gstRegistrationKnown,
    taxCodeTouched,
    value.tax_code,
  ]);

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
        <Field label={gstRegistered ? "Subtotal (ex GST)" : "Amount (no GST split)"}>
          <input
            className="input"
            inputMode="decimal"
            value={value.subtotal}
            onChange={(e) => set("subtotal", e.target.value)}
          />
        </Field>
        <Field label={gstRegistered ? "GST" : "GST (not registered)"}>
          <input
            className="input"
            inputMode="decimal"
            value={value.gst_amount}
            disabled={!gstRegistered}
            onChange={(e) => {
              const nextGst = e.target.value;
              if (
                Number(stripMoney(nextGst) || 0) > 0 &&
                ["gst_free", "input_taxed", "none"].includes(value.tax_code)
              ) {
                setTaxCodeTouched(false);
                const gross = Number(stripMoney(value.total));
                const gst = Number(stripMoney(nextGst));
                onChange({
                  ...value,
                  gst_amount: nextGst,
                  tax_code: "standard",
                  subtotal:
                    Number.isFinite(gross) &&
                    Number.isFinite(gst) &&
                    gross >= gst
                      ? (gross - gst).toFixed(2)
                      : value.subtotal,
                });
              } else {
                set("gst_amount", nextGst);
              }
            }}
          />
        </Field>
        <Field label={gstRegistered ? "Total (incl GST)" : "Total"}>
          <input
            className="input"
            inputMode="decimal"
            value={value.total}
            onChange={(e) => set("total", e.target.value)}
          />
        </Field>
      </div>

      <Field
        label={value.direction === "AR" ? "Income account" : "Expense / asset account"}
        hint="(needed to post to the ledger)"
      >
        <select
          className="input"
          value={value.account_id === "" ? "" : String(value.account_id)}
          onChange={(e) => {
            const nextId = e.target.value === "" ? "" : Number(e.target.value);
            const nextAccount =
              nextId === ""
                ? null
                : accountChoices.find((account) => account.id === nextId) ?? null;
            if (value.tax_code === "capital" && nextAccount?.type !== "ASSET") {
              setTaxCodeTouched(false);
              onChange({
                ...value,
                account_id: nextId,
                tax_code: defaultLineTaxCode(value.gst_amount),
              });
            } else {
              set("account_id", nextId);
            }
          }}
        >
          <option value="">— code later (stays a draft) —</option>
          {accountChoices.map((a) => (
            <option key={a.id} value={a.id}>
              {a.code} · {a.name}
            </option>
          ))}
        </select>
      </Field>

      <Field label="GST treatment" hint="(Australian tax classification)">
        <select
          className="input"
          value={gstRegistered ? value.tax_code : "none"}
          disabled={!gstRegistered}
          onChange={(event) => {
            const taxCode = event.target.value as TaxCode;
            setTaxCodeTouched(true);
            if (["gst_free", "input_taxed", "none"].includes(taxCode)) {
              const gross = stripMoney(value.total) || stripMoney(value.subtotal) || "0";
              onChange({
                ...value,
                tax_code: taxCode,
                subtotal: gross,
                gst_amount: "0",
                total: gross,
                gst_inclusive: false,
              });
            } else {
              onChange({ ...value, tax_code: taxCode });
            }
          }}
        >
          <option value="standard">GST taxable - standard rate (10%)</option>
          <option value="gst_free">GST-free sale or purchase</option>
          <option value="input_taxed">Input-taxed supply or acquisition</option>
          <option value="none">Outside GST / not reportable on BAS</option>
          {capitalEligible && (
            <option value="capital">Capital purchase - asset account (G10)</option>
          )}
        </select>
        {value.direction === "AP" && !capitalEligible && gstRegistered && (
          <span className="block text-xs text-slate-500 mt-1">
            Choose an Asset account to enable the Capital purchase treatment.
          </span>
        )}
        {!gstRegistered && gstRegistrationKnown && (
          <span className="block text-xs text-amber-700 mt-1">
            Non-GST-registered companies always submit Outside GST / none.
          </span>
        )}
      </Field>

      <div className="flex items-center gap-4 text-xs text-slate-600">
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={value.gst_inclusive}
            disabled={!gstRegistered}
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
        {!gstRegistered && gstRegistrationKnown && (
          <span className="text-amber-700">
            Company is not GST-registered; enter the full gross amount.
          </span>
        )}
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
