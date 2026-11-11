import type { Account, InvoiceDirection, TaxCode } from "../../types/api";
import type { InvoiceLineAmounts, InvoiceLineFormValue } from "./InvoiceForm";

const TAX_OPTIONS: Array<{ value: TaxCode; label: string }> = [
  { value: "standard", label: "Standard" },
  { value: "gst_free", label: "GST-free" },
  { value: "input_taxed", label: "Input-taxed" },
  { value: "none", label: "Outside GST" },
];

function amountLabel(amount: string | null | undefined): string {
  return amount ?? "—";
}

interface Props {
  lines: InvoiceLineFormValue[];
  amounts: ReadonlyMap<string, InvoiceLineAmounts | null>;
  accounts: Account[];
  direction: InvoiceDirection;
  gstRegistered: boolean;
  onChange: (
    id: string,
    updates: Partial<Omit<InvoiceLineFormValue, "id">>,
  ) => void;
  onAdd: () => void;
  onRemove: (id: string) => void;
}

export default function InvoiceLineTable({
  lines,
  amounts,
  accounts,
  direction,
  gstRegistered,
  onChange,
  onAdd,
  onRemove,
}: Props) {
  const taxOptions = (line: InvoiceLineFormValue) => {
    const selectedAccount = accounts.find((account) => account.id === line.account_id);
    return direction === "AP" && selectedAccount?.type === "ASSET"
      ? [...TAX_OPTIONS.slice(0, 3), { value: "capital" as const, label: "Capital purchase" }, TAX_OPTIONS[3]]
      : TAX_OPTIONS;
  };

  const accountSelect = (line: InvoiceLineFormValue) => (
    <select
      className="input min-w-0"
      aria-label="Account"
      value={line.account_id === "" ? "" : String(line.account_id)}
      onChange={(event) => onChange(line.id, {
        account_id: event.target.value ? Number(event.target.value) : "",
        ...(line.tax_code === "capital" &&
          accounts.find((account) => account.id === Number(event.target.value))?.type !== "ASSET"
          ? { tax_code: "gst_free" as TaxCode }
          : {}),
      })}
    >
      <option value="">Select account</option>
      {accounts.map((account) => (
        <option key={account.id} value={account.id}>
          {account.code} · {account.name}
        </option>
      ))}
    </select>
  );

  const taxSelect = (line: InvoiceLineFormValue) => (
    <select
      className="input min-w-0"
      aria-label="Tax rate"
      value={gstRegistered ? line.tax_code : "none"}
      disabled={!gstRegistered}
      onChange={(event) => onChange(line.id, { tax_code: event.target.value as TaxCode })}
    >
      {taxOptions(line).map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );

  const removeButton = (line: InvoiceLineFormValue) => (
    <button
      type="button"
      className="text-sm text-red-700 hover:text-red-900 disabled:text-slate-400"
      aria-label="Remove line"
      title={lines.length === 1 ? "At least one line is required" : "Remove line"}
      disabled={lines.length === 1}
      onClick={() => onRemove(line.id)}
    >
      Remove
    </button>
  );

  return (
    <section aria-label="Invoice lines" className="space-y-2">
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[1100px] table-fixed text-sm">
          <colgroup>
            <col className="w-[24%]" />
            <col className="w-[6%]" />
            <col className="w-[10%]" />
            <col className="w-[18%]" />
            <col className="w-[14%]" />
            <col className="w-[8%]" />
            <col className="w-[7%]" />
            <col className="w-[8%]" />
            <col className="w-[5%]" />
          </colgroup>
          <thead>
            <tr className="border-b border-slate-200 text-left text-xs text-slate-600">
              <th className="px-2 py-2 font-medium">Description</th>
              <th className="px-2 py-2 font-medium">Qty</th>
              <th className="px-2 py-2 font-medium">Unit price</th>
              <th className="px-2 py-2 font-medium">Account</th>
              <th className="px-2 py-2 font-medium">Tax rate</th>
              <th className="px-2 py-2 text-right font-medium">Subtotal</th>
              <th className="px-2 py-2 text-right font-medium">GST</th>
              <th className="px-2 py-2 text-right font-medium">Total</th>
              <th className="px-2 py-2 text-right font-medium">Remove</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line) => (
              <tr key={line.id} data-line-id={line.id} className="border-b border-slate-100 align-top">
                <td className="px-1 py-2">
                  <input
                    className="input min-w-0"
                    aria-label="Description"
                    value={line.description}
                    onChange={(event) => onChange(line.id, { description: event.target.value })}
                  />
                </td>
                <td className="px-1 py-2">
                  <input
                    className="input min-w-0"
                    aria-label="Qty"
                    inputMode="decimal"
                    value={line.quantity}
                    onChange={(event) => onChange(line.id, { quantity: event.target.value })}
                  />
                </td>
                <td className="px-1 py-2">
                  <input
                    className="input min-w-0"
                    aria-label="Unit price"
                    inputMode="decimal"
                    value={line.unit_price}
                    onChange={(event) => onChange(line.id, { unit_price: event.target.value })}
                  />
                </td>
                <td className="px-1 py-2">{accountSelect(line)}</td>
                <td className="px-1 py-2">{taxSelect(line)}</td>
                <td className="px-2 py-2 text-right tabular-nums" aria-label="Line subtotal">
                  {amountLabel(amounts.get(line.id)?.subtotal)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums" aria-label="Line GST">
                  {amountLabel(amounts.get(line.id)?.gst)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums" aria-label="Line total">
                  {amountLabel(amounts.get(line.id)?.total)}
                </td>
                <td className="px-2 py-2 text-right">{removeButton(line)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3 md:hidden">
        {lines.map((line) => (
          <div key={line.id} data-line-id={line.id} className="space-y-3 border-b border-slate-200 py-3">
            <label className="block text-sm">
              <span className="mb-1 block text-slate-600">Description</span>
              <input
                className="input"
                aria-label="Description"
                value={line.description}
                onChange={(event) => onChange(line.id, { description: event.target.value })}
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="mb-1 block text-slate-600">Qty</span>
                <input
                  className="input"
                  aria-label="Qty"
                  inputMode="decimal"
                  value={line.quantity}
                  onChange={(event) => onChange(line.id, { quantity: event.target.value })}
                />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block text-slate-600">Unit price</span>
                <input
                  className="input"
                  aria-label="Unit price"
                  inputMode="decimal"
                  value={line.unit_price}
                  onChange={(event) => onChange(line.id, { unit_price: event.target.value })}
                />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block text-slate-600">Account</span>
                {accountSelect(line)}
              </label>
              <label className="block text-sm">
                <span className="mb-1 block text-slate-600">Tax rate</span>
                {taxSelect(line)}
              </label>
            </div>
            <div className="grid grid-cols-3 gap-3 text-sm text-right tabular-nums">
              <div>
                <span className="block text-slate-600">Subtotal</span>
                <span>{amountLabel(amounts.get(line.id)?.subtotal)}</span>
              </div>
              <div>
                <span className="block text-slate-600">GST</span>
                <span>{amountLabel(amounts.get(line.id)?.gst)}</span>
              </div>
              <div>
                <span className="block text-slate-600">Total</span>
                <span>{amountLabel(amounts.get(line.id)?.total)}</span>
              </div>
            </div>
            <div className="flex justify-end">{removeButton(line)}</div>
          </div>
        ))}
      </div>

      <div className="flex justify-start">
        <button type="button" className="btn-secondary" onClick={onAdd}>Add line</button>
      </div>
    </section>
  );
}
