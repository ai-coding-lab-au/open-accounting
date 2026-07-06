import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import OutgoingEditDialog from "./OutgoingEditDialog";
import { useCompanyStore } from "../../store/company";
import { usePrivacyEnabled } from "../../lib/usePrivacy";
import type { OutgoingDocument } from "../../types/api";
import { displayDocNumber, displayName, formatDate, formatMoney, statusBadgeClass, statusLabel } from "../../lib/format";

async function fetchPdfBlob(id: number, companyId: string): Promise<Blob> {
  const res = await api.post(`/outgoing/${id}/pdf`, null, {
    params: { inline: true },
    responseType: "blob",
    headers: { "X-Company-Id": companyId },
  });
  return res.data as Blob;
}

async function fetchOutgoingDoc(id: number): Promise<OutgoingDocument> {
  const { data } = await api.get<OutgoingDocument>(`/outgoing/${id}`);
  return data;
}

async function voidDoc(id: number) {
  await api.delete(`/outgoing/${id}`);
}

async function restoreDoc(id: number): Promise<OutgoingDocument> {
  const { data } = await api.post<OutgoingDocument>(`/outgoing/${id}/restore`);
  return data;
}

export default function OutgoingDetailDrawer({
  doc,
  onClose,
}: {
  doc: OutgoingDocument;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const companyId = useCompanyStore((s) => s.currentId);
  const privacyOn = usePrivacyEnabled();
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEdit, setShowEdit] = useState(false);
  const [confirmVoid, setConfirmVoid] = useState(false);
  const detailQuery = useQuery({
    // Keyed by company id too (like the list queries) so a company switch
    // never flashes another company's cached document PII.
    queryKey: ["outgoing-detail", companyId, doc.id],
    queryFn: () => fetchOutgoingDoc(doc.id),
    retry: false,
  });
  const currentDoc = detailQuery.data ?? doc;
  const actionLoading = detailQuery.isFetching;

  useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    let url: string | null = null;
    setLoading(true);
    setError(null);
    fetchPdfBlob(doc.id, companyId)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setPdfUrl(url);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(apiErrorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [doc.id, companyId, currentDoc.updated_at]);

  const restoreMut = useMutation({
    mutationFn: () => restoreDoc(doc.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["outgoing"] });
      qc.invalidateQueries({ queryKey: ["outgoing-detail", companyId, doc.id] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });

  const voidMut = useMutation({
    mutationFn: () => voidDoc(doc.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["outgoing"] });
      qc.invalidateQueries({ queryKey: ["outgoing-detail", companyId, doc.id] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });

  const canEdit = currentDoc.status !== "void";

  useModalKeys({ open: !showEdit, onClose });

  // Close on a backdrop click only when the press AND the release both land on
  // the backdrop itself — so a double-click that opened this drawer, or a
  // drag-select that ends outside, can't accidentally dismiss it.
  const backdropDown = useRef(false);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/35 p-6 flex items-center justify-center"
      onMouseDown={(e) => {
        backdropDown.current = e.target === e.currentTarget;
      }}
      onMouseUp={(e) => {
        if (backdropDown.current && e.target === e.currentTarget) onClose();
        backdropDown.current = false;
      }}
    >
      <div
        role="dialog"
        aria-label={`Receipt ${currentDoc.doc_number}`}
        className="w-[min(1200px,calc(100vw-48px))] h-[min(900px,calc(100vh-48px))] bg-surface border border-slate-200 flex flex-col overflow-hidden"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <div className="text-xs text-slate-500">Receipt</div>
            <div className="text-lg font-semibold font-mono">{displayDocNumber(currentDoc.doc_number)}</div>
          </div>
          <div className="flex items-center gap-2">
            <span className={`text-xs px-2 py-0.5 rounded ${statusBadgeClass(currentDoc.status)}`}>
              {statusLabel(currentDoc.status)}
            </span>
            {canEdit && (
              <button
                className="px-2 rounded border border-slate-300 text-slate-700 hover:bg-slate-100 disabled:opacity-60"
                style={{ fontSize: 12, lineHeight: "16px", height: 24 }}
                onClick={() => setShowEdit(true)}
                disabled={actionLoading}
              >
                Edit
              </button>
            )}
            {currentDoc.status === "void" ? (
              <button
                className="px-2 rounded border border-slate-300 text-slate-700 hover:bg-slate-100 disabled:opacity-60"
                style={{ fontSize: 12, lineHeight: "16px", height: 24 }}
                onClick={() => restoreMut.mutate()}
                disabled={actionLoading || restoreMut.isPending}
              >
                {restoreMut.isPending ? "..." : "Restore"}
              </button>
            ) : (
              <button
                className="px-2 rounded border border-rose-200 text-rose-700 hover:bg-rose-50 disabled:opacity-60"
                style={{ fontSize: 12, lineHeight: "16px", height: 24 }}
                onClick={() => setConfirmVoid(true)}
                disabled={actionLoading || voidMut.isPending}
              >
                {voidMut.isPending ? "..." : "Void"}
              </button>
            )}
            <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
              ×
            </button>
          </div>
        </div>

        <div className="px-5 py-3 border-b border-slate-200 text-sm grid grid-cols-3 gap-3">
          <Info label="Customer">{displayName(currentDoc.customer_name, "client")}</Info>
          <Info label="Issue date">{formatDate(currentDoc.issue_date)}</Info>
          <Info label="Total">{formatMoney(currentDoc.total, currentDoc.currency)}</Info>
        </div>

        <div className="flex-1 overflow-hidden bg-slate-100">
          {loading && (
            <div className="h-full flex items-center justify-center text-sm text-slate-500">
              Rendering PDF…
            </div>
          )}
          {error && (
            <div className="h-full flex items-center justify-center text-sm text-red-600 px-6 text-center">
              {error}
            </div>
          )}
          {!loading && !error && privacyOn && (
            <div className="h-full flex flex-col items-center justify-center gap-1 px-6 text-center text-sm text-slate-500">
              <div className="font-medium">Preview hidden in privacy mode</div>
              <div>
                The receipt PDF shows client details. Turn privacy off, or use
                Download, to view it.
              </div>
            </div>
          )}
          {pdfUrl && !loading && !privacyOn && (
            <iframe
              src={pdfUrl}
              title={currentDoc.doc_number}
              className="w-full h-full border-0 bg-surface"
            />
          )}
        </div>

        {restoreMut.isError && (
          <div className="px-5 py-2 text-sm text-rose-600 bg-rose-50 border-t border-rose-200">
            {apiErrorMessage(restoreMut.error)}
          </div>
        )}
        {voidMut.isError && (
          <div className="px-5 py-2 text-sm text-rose-600 bg-rose-50 border-t border-rose-200">
            {apiErrorMessage(voidMut.error)}
          </div>
        )}
        <div className="px-5 py-3 border-t border-slate-200 flex items-center justify-end gap-2">
          {pdfUrl && (
            <a
              className="btn-secondary"
              href={pdfUrl}
              download={`${displayDocNumber(currentDoc.doc_number)}.pdf`}
            >
              Download PDF
            </a>
          )}
        </div>
      </div>

      {showEdit && (
        <OutgoingEditDialog
          doc={currentDoc}
          onClose={() => setShowEdit(false)}
          onSaved={(updated) => {
            qc.setQueryData(["outgoing-detail", companyId, updated.id], updated);
            setShowEdit(false);
          }}
        />
      )}

      <ConfirmDialog
        open={confirmVoid}
        destructive
        title="Void this receipt?"
        message={`Void ${displayDocNumber(currentDoc.doc_number)}? It will be marked void. You can restore it later.`}
        confirmLabel="Void"
        busy={voidMut.isPending}
        onCancel={() => setConfirmVoid(false)}
        onConfirm={() => {
          setConfirmVoid(false);
          voidMut.mutate();
        }}
      />
    </div>
  );
}

function Info({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="font-medium truncate">{children}</div>
    </div>
  );
}
