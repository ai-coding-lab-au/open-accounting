import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import type { Invoice, InvoiceDirection, InvoiceStatus } from "../../types/api";
import {
  displayDocNumber,
  displayName,
  formatDate,
  formatMoney,
  statusBadgeClass,
} from "../../lib/format";

type DirectionFilter = "ALL" | InvoiceDirection;
type StatusFilter = "ALL" | InvoiceStatus;

async function fetchInvoices(params: {
  direction?: InvoiceDirection;
  status?: InvoiceStatus;
  q?: string;
}): Promise<Invoice[]> {
  const { data } = await api.get<Invoice[]>("/invoices", { params });
  return data;
}

export default function InvoicesContent({
  onSelectInvoice,
  onCreateManual,
  onImportExcel,
  showCreateActions = true,
  title = "Invoices",
}: {
  onSelectInvoice: (invoice: Invoice) => void;
  onCreateManual: () => void;
  onImportExcel: () => void;
  showCreateActions?: boolean;
  title?: string;
}) {
  const currentId = useCompanyStore((s) => s.currentId);
  const [direction, setDirection] = useState<DirectionFilter>("ALL");
  const [status, setStatus] = useState<StatusFilter>("ALL");
  const [q, setQ] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["invoices", currentId, direction, status, q],
    queryFn: () =>
      fetchInvoices({
        direction: direction === "ALL" ? undefined : direction,
        status: status === "ALL" ? undefined : status,
        q: q.trim() || undefined,
      }),
    enabled: !!currentId,
  });

  const totals = useMemo(() => {
    if (!data) return { count: 0, total: 0, unpaid: 0 };
    let total = 0;
    let unpaid = 0;
    for (const inv of data) {
      if (inv.status === "void") continue;
      total += Number(inv.total);
      unpaid += Number(inv.total) - Number(inv.paid_amount);
    }
    return { count: data.length, total, unpaid };
  }, [data]);

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">Pick a company in the top bar.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold">{title}</h1>
        {showCreateActions && (
          <div className="flex gap-2">
            <button className="btn-secondary" onClick={onCreateManual}>
              + Manual
            </button>
            <button className="btn-secondary" onClick={onImportExcel}>
              Import Excel/CSV
            </button>
          </div>
        )}
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 p-4">
        <div className="flex flex-wrap gap-3 mb-3">
          <Pills
            value={direction}
            onChange={(v) => setDirection(v as DirectionFilter)}
            options={[
              { v: "ALL", label: "All" },
              { v: "AP", label: "AP (bills)" },
              { v: "AR", label: "AR (sales)" },
            ]}
          />
          <Pills
            value={status}
            onChange={(v) => setStatus(v as StatusFilter)}
            options={[
              { v: "ALL", label: "Any status" },
              { v: "unpaid", label: "Unpaid" },
              { v: "partial", label: "Partial" },
              { v: "paid", label: "Paid" },
              { v: "void", label: "Void" },
            ]}
          />
          <input
            className="input w-64"
            placeholder="Search invoice # or contact…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>

        {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
        {error && <p className="text-sm text-red-600">{(error as Error).message}</p>}

        {data && data.length === 0 && (
          <div className="text-center text-sm text-slate-500 py-10">
            No invoices yet. Use "Import Excel/CSV" to bring some in, or click
            "+ Manual" to type one in.
          </div>
        )}

        {data && data.length > 0 && (
          <>
            <div className="text-xs text-slate-500 mb-2">
              Showing <span className="font-semibold">{totals.count}</span> · total{" "}
              <span className="font-semibold">{formatMoney(totals.total)}</span> · outstanding{" "}
              <span className="font-semibold">{formatMoney(totals.unpaid)}</span>
            </div>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-slate-500 border-b">
                  <tr>
                    <Th>Date</Th>
                    <Th>Dir</Th>
                    <Th>Invoice #</Th>
                    <Th>Contact</Th>
                    <Th className="text-right">Subtotal</Th>
                    <Th className="text-right">GST</Th>
                    <Th className="text-right">Total</Th>
                    <Th>Status</Th>
                    <Th>Src</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((inv) => (
                    <tr
                      key={inv.id}
                      className="border-b last:border-b-0 hover:bg-slate-50 cursor-pointer"
                      onClick={() => onSelectInvoice(inv)}
                    >
                      <Td>{formatDate(inv.issue_date)}</Td>
                      <Td>
                        <span
                          className={`text-xs px-1.5 py-0.5 rounded ${
                            inv.direction === "AP"
                              ? "bg-orange-100 text-orange-800"
                              : "bg-sky-100 text-sky-800"
                          }`}
                        >
                          {inv.direction}
                        </span>
                      </Td>
                      <Td className="font-mono">{displayDocNumber(inv.invoice_number)}</Td>
                      <Td>{displayName(inv.contact_name, "provider")}</Td>
                      <Td className="text-right">
                        {formatMoney(inv.subtotal, inv.currency)}
                      </Td>
                      <Td className="text-right">
                        {formatMoney(inv.gst_amount, inv.currency)}
                      </Td>
                      <Td className="text-right font-medium">
                        {formatMoney(inv.total, inv.currency)}
                      </Td>
                      <Td>
                        <span
                          className={`text-xs px-2 py-0.5 rounded ${statusBadgeClass(inv.status)}`}
                        >
                          {inv.status}
                        </span>
                      </Td>
                      <Td className="text-xs text-slate-500">
                        {inv.source}
                        {inv.attachments.length > 0 && " 📎"}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Pills({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { v: string; label: string }[];
}) {
  return (
    <div className="inline-flex rounded border border-slate-300 overflow-hidden text-sm">
      {options.map((o) => (
        <button
          key={o.v}
          className={`px-3 py-1 ${
            value === o.v ? "bg-slate-900 text-white" : "bg-surface text-slate-700 hover:bg-slate-100"
          }`}
          onClick={() => onChange(o.v)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Th({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <th className={`py-1 px-2 ${className}`}>{children}</th>;
}

function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={`py-1.5 px-2 ${className}`}>{children}</td>;
}
