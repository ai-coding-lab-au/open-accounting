import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Client, OutgoingCreate, OutgoingDocType } from "../../types/api";
import { formatMoney } from "../../lib/format";
import { todayLocal } from "../../lib/date";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import DateInput from "../DateInput";
import ClientSelect from "../clients/ClientSelect";
import { blockScientificNotation } from "../../lib/numericInput";

export interface LineRow {
  description: string;
  quantity: string;
  unit_price: string;
  amount: string;
}

export interface EditorValues {
  doc_type: OutgoingDocType;
  issue_date: string;
  client_ref_id: number | null;
  customer_name: string;
  customer_address: string;
  customer_email: string;
  customer_phone: string;
  currency: string;
  notes: string;
  payment_method: string;
  paid_date: string;
  doc_number_override: string;
  lines: LineRow[];
}

export const EMPTY_LINE: LineRow = {
  description: "",
  quantity: "1",
  unit_price: "0",
  amount: "",
};

export function emptyEditorValues(
  doc_type: OutgoingDocType = "receipt",
  _defaultPaymentTermsDays = 0,
): EditorValues {
  const today = todayLocal();
  return {
    doc_type,
    issue_date: today,
    client_ref_id: null,
    customer_name: "",
    customer_address: "",
    customer_email: "",
    customer_phone: "",
    currency: "AUD",
    notes: "",
    payment_method: "Bank transfer",
    paid_date: today,
    doc_number_override: "",
    lines: [{ ...EMPTY_LINE }],
  };
}

export function editorValuesFromDocument(doc: import("../../types/api").OutgoingDocument): EditorValues {
  return {
    doc_type: doc.doc_type,
    issue_date: doc.issue_date,
    client_ref_id: doc.client_ref_id,
    customer_name: doc.customer_name ?? "",
    customer_address: doc.customer_address ?? "",
    customer_email: doc.customer_email ?? "",
    customer_phone: doc.customer_phone ?? "",
    currency: doc.currency || "AUD",
    notes: doc.notes ?? "",
    payment_method: doc.payment_method ?? "",
    paid_date: doc.paid_date ?? "",
    doc_number_override: "",
    lines: doc.lines.length > 0
      ? doc.lines.map((l) => ({
          description: l.description,
          quantity: String(l.quantity),
          unit_price: String(l.unit_price),
          amount: l.amount != null ? String(l.amount) : "",
        }))
      : [{ ...EMPTY_LINE }],
  };
}

export function toCreatePayload(v: EditorValues): OutgoingCreate {
  return {
    doc_type: v.doc_type,
    ...toUpdatePayload(v),
    doc_number_override: v.doc_number_override.trim() || null,
  };
}

export function toUpdatePayload(v: EditorValues) {
  return {
    issue_date: v.issue_date,
    client_ref_id: v.client_ref_id,
    customer_name: v.customer_name.trim() || null,
    customer_address: v.customer_address.trim() || null,
    customer_email: v.customer_email.trim() || null,
    customer_phone: v.customer_phone.trim() || null,
    currency: v.currency || "AUD",
    notes: v.notes.trim() || null,
    payment_method: v.payment_method.trim() || null,
    paid_date: v.paid_date || null,
    lines: v.lines
      .filter((l) => l.description.trim() || l.amount || l.unit_price !== "0")
      .map((l) => ({
        description: l.description.trim(),
        quantity: l.quantity || "1",
        unit_price: l.unit_price || "0",
        amount: l.amount || undefined,
      })),
  };
}

// Once a document leaves DRAFT, the backend locks every field except `notes`
// (payee, currency, lines/totals, issue date — void and re-create instead).
// Submitting the full toUpdatePayload() there always 409s, even when only
// notes changed, because it always includes `lines`. Send notes-only instead.
export function toNotesOnlyPayload(v: EditorValues) {
  return { notes: v.notes.trim() || null };
}

function lineComputedAmount(l: LineRow): number {
  if (l.amount && l.amount.trim() !== "") {
    const n = Number(l.amount);
    if (!Number.isNaN(n)) return n;
  }
  const q = Number(l.quantity);
  const u = Number(l.unit_price);
  if (Number.isNaN(q) || Number.isNaN(u)) return 0;
  return q * u;
}

const round2 = (n: number) => Math.round((n + Number.EPSILON) * 100) / 100;

// Cap the number of decimal places a money/quantity input accepts, so the live
// preview matches what the backend will store (MONEY = 2dp, quantity = 4dp) and
// the user isn't surprised by a "no more than 2 decimal places" error only on
// submit.
function limitDecimals(v: string, places: number): string {
  const m = /^(-?\d*)(?:\.(\d*))?$/.exec(v);
  if (!m) return v;
  if (m[2] != null && m[2].length > places) {
    return places === 0 ? m[1] : `${m[1]}.${m[2].slice(0, places)}`;
  }
  return v;
}

export default function OutgoingEditor({
  value,
  onChange,
  showDocNumberOverride = true,
  locked = false,
}: {
  value: EditorValues;
  onChange: (v: EditorValues) => void;
  defaultPaymentTermsDays?: number;
  showDocNumberOverride?: boolean;
  // Once issued, everything except notes is locked server-side (void and
  // re-create to change payee/currency/amounts/date). Render those fields
  // read-only so the form matches what a save will actually accept.
  locked?: boolean;
}) {
  const update = <K extends keyof EditorValues>(k: K, v: EditorValues[K]) =>
    onChange({ ...value, [k]: v });

  const pickClient = (client: Client | null) => {
    onChange({
      ...value,
      client_ref_id: client?.id ?? null,
      customer_name: client?.display_name ?? "",
      customer_address: client?.address ?? "",
      customer_email: client?.email ?? "",
      customer_phone: client?.phone ?? "",
    });
  };

  const updateLine = (i: number, patch: Partial<LineRow>) => {
    const next = value.lines.map((l, idx) => (idx === i ? { ...l, ...patch } : l));
    onChange({ ...value, lines: next });
  };

  const addLine = () =>
    onChange({ ...value, lines: [...value.lines, { ...EMPTY_LINE }] });

  const removeLine = (i: number) => {
    if (value.lines.length === 1) {
      onChange({ ...value, lines: [{ ...EMPTY_LINE }] });
      return;
    }
    onChange({ ...value, lines: value.lines.filter((_, idx) => idx !== i) });
  };

  // Round each line to 2dp first (mirroring the backend), so the displayed
  // subtotal / GST / total always add up on screen instead of drifting a cent.
  const subtotal = useMemo(
    () => round2(value.lines.reduce((acc, l) => acc + round2(lineComputedAmount(l)), 0)),
    [value.lines]
  );

  const currentCompanyId = useCompanyStore((s) => s.currentId);
  const { data: companies } = useQuery({
    queryKey: ["companies"],
    queryFn: async () =>
      (await api.get<Array<{ id: string; gst_registered: boolean }>>("/companies")).data,
    staleTime: 60_000,
  });
  const gstRegistered = !!companies?.find((c) => c.id === currentCompanyId)?.gst_registered;
  const gstAmount = gstRegistered ? round2(subtotal * 0.1) : 0;
  const total = round2(subtotal + gstAmount);

  return (
    <div className="space-y-5">
      {locked && (
        <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2">
          Only notes can be edited once issued. Void this document and create a
          replacement to change the payee, currency, amounts, or date.
        </div>
      )}
      {/* Doc-level fields */}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Document type">
          <input className="input" value="Receipt" readOnly />
        </Field>
        <Field label="Currency">
          {locked ? (
            <input className="input" value={value.currency} readOnly />
          ) : (
            <input
              className="input"
              value={value.currency}
              onChange={(e) => update("currency", e.target.value.toUpperCase())}
              maxLength={3}
            />
          )}
        </Field>
        <Field label="Issue date" hint="(DD/MM/YYYY)">
          <DateInput
            value={value.issue_date}
            onChange={(v) => update("issue_date", v)}
            disabled={locked}
          />
        </Field>
        <Field label="Paid date" hint="(DD/MM/YYYY)">
          <DateInput
            value={value.paid_date}
            onChange={(v) => update("paid_date", v)}
            disabled={locked}
          />
        </Field>
        {showDocNumberOverride && (
          <Field label="Number (optional)" hint="prefix & year added automatically">
            <input
              className="input font-mono"
              placeholder="auto"
              inputMode="numeric"
              value={value.doc_number_override}
              onChange={(e) => update("doc_number_override", e.target.value)}
            />
          </Field>
        )}
        <Field label="Payment method">
          {locked ? (
            <input className="input" value={value.payment_method} readOnly />
          ) : (
            <input
              className="input"
              placeholder="e.g. Bank transfer"
              value={value.payment_method}
              onChange={(e) => update("payment_method", e.target.value)}
            />
          )}
        </Field>
      </div>

      {/* Customer */}
      <fieldset className="border border-slate-200 rounded p-3 space-y-3">
        <legend className="text-xs font-semibold text-slate-600 px-1">Bill to</legend>
        {locked ? (
          <Field label="Client">
            <input className="input" value={value.customer_name} readOnly />
          </Field>
        ) : (
          <ClientSelect
            label="Client"
            selectedId={value.client_ref_id}
            selectedName={value.customer_name}
            onPick={pickClient}
          />
        )}
        <Field label="Address from client record">
          <textarea className="input min-h-[60px]" value={value.customer_address} readOnly />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Email">
            <input className="input" value={value.customer_email} readOnly />
          </Field>
          <Field label="Phone">
            <input className="input" value={value.customer_phone} readOnly />
          </Field>
        </div>
      </fieldset>

      {/* Line items */}
      <fieldset className="border border-slate-200 rounded p-3">
        <legend className="text-xs font-semibold text-slate-600 px-1">Line items</legend>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="py-1 px-1">Description</th>
              <th className="py-1 px-1 w-20 text-right">Qty</th>
              <th className="py-1 px-1 w-28 text-right">Unit price</th>
              <th className="py-1 px-1 w-32 text-right">Amount</th>
              <th className="py-1 px-1 w-8"></th>
            </tr>
          </thead>
          <tbody>
            {value.lines.map((l, i) => (
              <tr key={i} className="border-t border-slate-100">
                <td className="py-1 px-1">
                  <input
                    className="input"
                    value={l.description}
                    onChange={(e) => updateLine(i, { description: e.target.value })}
                    readOnly={locked}
                  />
                </td>
                <td className="py-1 px-1">
                  <input
                    className="input text-right"
                    type="number"
                    min="0"
                    step="1"
                    value={l.quantity}
                    onChange={(e) => updateLine(i, { quantity: limitDecimals(e.target.value, 4) })}
                    onKeyDown={blockScientificNotation}
                    readOnly={locked}
                  />
                </td>
                <td className="py-1 px-1">
                  <input
                    className="input text-right"
                    type="number"
                    min="0"
                    step="0.01"
                    value={l.unit_price}
                    onChange={(e) => updateLine(i, { unit_price: limitDecimals(e.target.value, 2) })}
                    onKeyDown={blockScientificNotation}
                    readOnly={locked}
                  />
                </td>
                <td className="py-1 px-1">
                  <input
                    className="input text-right"
                    type="number"
                    min="0"
                    step="0.01"
                    placeholder={(
                      (Number(l.quantity) || 0) * (Number(l.unit_price) || 0)
                    ).toFixed(2)}
                    value={l.amount}
                    onChange={(e) => updateLine(i, { amount: limitDecimals(e.target.value, 2) })}
                    onKeyDown={blockScientificNotation}
                    readOnly={locked}
                  />
                </td>
                <td className="py-1 px-1 text-right">
                  {!locked && (
                    <button
                      type="button"
                      className="text-slate-400 hover:text-rose-600"
                      onClick={() => removeLine(i)}
                      title="Remove line"
                    >
                      ×
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!locked && (
          <button
            type="button"
            className="mt-2 text-sm text-sky-700 hover:text-sky-900"
            onClick={addLine}
          >
            + Add line
          </button>
        )}
        <div className="mt-3 flex justify-end text-sm">
          <div className="w-64 space-y-1">
            <div className="flex justify-between">
              <span className="text-slate-500">
                {gstRegistered ? "Total (excl. GST)" : "Subtotal"}
              </span>
              <span className="font-medium">{formatMoney(subtotal, value.currency)}</span>
            </div>
            <div className="flex justify-between text-xs text-slate-500">
              <span>GST {gstRegistered ? "(10%)" : ""}</span>
              <span>
                {gstRegistered ? formatMoney(gstAmount, value.currency) : "Not registered (N/A)"}
              </span>
            </div>
            <div className="flex justify-between border-t pt-1">
              <span className="font-semibold">
                {gstRegistered ? "Total (incl. GST)" : "Total"}
              </span>
              <span className="font-semibold">{formatMoney(total, value.currency)}</span>
            </div>
          </div>
        </div>
      </fieldset>

      <Field label="Notes">
        <textarea
          className="input min-h-[60px]"
          value={value.notes}
          onChange={(e) => update("notes", e.target.value)}
        />
      </Field>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs font-medium text-slate-600 mb-1">
        {label} {hint && <span className="text-slate-400 font-normal">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
