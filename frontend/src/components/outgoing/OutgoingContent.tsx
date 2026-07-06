import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import type {
  OutgoingDocStatus,
  OutgoingDocType,
  OutgoingDocument,
} from "../../types/api";
import {
  displayDocNumber,
  displayName,
  formatDate,
  formatMoney,
  statusBadgeClass,
  statusLabel,
} from "../../lib/format";

type StatusFilter = "ALL" | OutgoingDocStatus;

async function fetchOutgoing(params: {
  doc_type?: OutgoingDocType;
  status?: OutgoingDocStatus;
  q?: string;
}): Promise<OutgoingDocument[]> {
  const { data } = await api.get<OutgoingDocument[]>("/outgoing", { params });
  return data;
}

function lineSummary(doc: OutgoingDocument): string {
  const labels: string[] = [];
  for (const line of doc.lines ?? []) {
    const desc = (line.description ?? "").trim();
    if (desc && !labels.includes(desc)) labels.push(desc);
  }
  return labels.length ? labels.join(", ") : "—";
}

export default function OutgoingContent({
  title,
  fixedDocType = "receipt",
  createActions,
  onSelectDocument,
  showCreateActions = true,
}: {
  title: string;
  fixedDocType?: OutgoingDocType;
  createActions: { docType: OutgoingDocType; label: string; className: string; onClick: () => void }[];
  onSelectDocument: (doc: OutgoingDocument) => void;
  showCreateActions?: boolean;
}) {
  const currentId = useCompanyStore((s) => s.currentId);
  const [status, setStatus] = useState<StatusFilter>("ALL");
  const [q, setQ] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["outgoing", currentId, fixedDocType, status, q],
    queryFn: () =>
      fetchOutgoing({
        doc_type: fixedDocType,
        status: status === "ALL" ? undefined : status,
        q: q.trim() || undefined,
      }),
    enabled: !!currentId,
  });

  const totals = useMemo(() => {
    if (!data) return { count: 0, total: 0 };
    let total = 0;
    for (const d of data) {
      if (d.status === "void") continue;
      total += Number(d.total);
    }
    return { count: data.length, total };
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
        {showCreateActions && createActions.length > 0 && (
          <div className="flex gap-2">
            {createActions.map((action) => (
              <button
                key={action.docType}
                className={action.className}
                onClick={() => action.onClick?.()}
              >
                {action.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 p-4">
        <div className="flex flex-wrap gap-3 mb-3">
          <Pills
            value={status}
            onChange={(v) => setStatus(v as StatusFilter)}
            options={[
              { v: "ALL", label: "Any status" },
              { v: "issued", label: "Issued" },
              { v: "void", label: "Void" },
            ]}
          />
          <input
            className="input w-64"
            placeholder="Search number or client…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>

        {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
        {error && <p className="text-sm text-red-600">{(error as Error).message}</p>}

        {data && data.length === 0 && (
          <div className="text-center text-sm text-slate-500 py-10">
            No receipts yet. Click "+ Receipt" to create one.
          </div>
        )}

        {data && data.length > 0 && (
          <>
            <div className="text-xs text-slate-500 mb-2">
              Showing <span className="font-semibold">{totals.count}</span> · received{" "}
              <span className="font-semibold">{formatMoney(totals.total)}</span>
            </div>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-slate-500 border-b">
                  <tr>
                    <Th>Date</Th>
                    <Th>Number</Th>
                    <Th>Client</Th>
                    <Th>Items</Th>
                    <Th>Total</Th>
                    <Th>Status</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((d) => (
                    <tr
                      key={d.id}
                      className="border-b last:border-b-0 hover:bg-slate-50 cursor-pointer"
                      onClick={() => onSelectDocument(d)}
                    >
                      <Td>{formatDate(d.issue_date)}</Td>
                      <Td className="font-mono">{displayDocNumber(d.doc_number)}</Td>
                      <Td>{displayName(d.customer_name, "client")}</Td>
                      <Td>{lineSummary(d)}</Td>
                      <Td className="font-medium">{formatMoney(d.total, d.currency)}</Td>
                      <Td>
                        <span className={`text-xs px-2 py-0.5 rounded ${statusBadgeClass(d.status)}`}>
                          {statusLabel(d.status)}
                        </span>
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
