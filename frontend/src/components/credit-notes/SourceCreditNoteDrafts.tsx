import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { formatDate, formatMoney } from "../../lib/format";
import { useCompanyStore } from "../../store/company";
import type { CreditNote, CreditNoteSource } from "../../types/api";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import CreditNoteDraftDialog from "./CreditNoteDraftDialog";

export default function SourceCreditNoteDrafts({ invoiceId }: { invoiceId: number }) {
  const queryClient = useQueryClient();
  const currentId = useCompanyStore((state) => state.currentId);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CreditNote | null>(null);

  const snapshotQuery = useQuery({
    queryKey: ["credit-notes", "source-snapshot", currentId, invoiceId],
    queryFn: async () =>
      (await api.get<CreditNoteSource>(`/credit-notes/source-invoices/${invoiceId}`)).data,
    enabled: !!currentId,
    retry: false,
  });
  const draftsQuery = useQuery({
    queryKey: ["credit-notes", "source-list", currentId, invoiceId],
    queryFn: async () =>
      (await api.get<CreditNote[]>("/credit-notes", {
        params: { source_invoice_id: invoiceId },
      })).data,
    enabled: !!currentId && !!snapshotQuery.data && !snapshotQuery.isError,
    retry: false,
  });

  const deleteMutation = useMutation({
    mutationFn: async (creditNoteId: number) => {
      await api.delete(`/credit-notes/${creditNoteId}`);
      return creditNoteId;
    },
    onSuccess: (creditNoteId) => {
      setDeleteTarget(null);
      setEditingId(null);
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-list", currentId, invoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-snapshot", currentId, invoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "detail", currentId, creditNoteId],
        exact: true,
      });
    },
  });

  const snapshot = snapshotQuery.data;
  const allRemainingReserved =
    !!snapshot &&
    snapshot.lines.length > 0 &&
    snapshot.lines.every((line) => /^0(?:\.0+)?$/.test(line.remaining_creditable_quantity));
  const snapshotReady = !!snapshot && !snapshotQuery.isError;
  const createDisabled = !snapshotReady || snapshotQuery.isFetching || allRemainingReserved;

  return (
    <section className="border border-slate-200 rounded p-3" aria-label="Credit notes for source invoice">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <h3 className="text-sm font-medium text-slate-900">Credit notes for this invoice</h3>
        {snapshotQuery.isFetching ? (
          <button type="button" className="btn-secondary text-xs" disabled>
            Checking eligibility…
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary text-xs"
            disabled={createDisabled}
            onClick={() => setShowCreate(true)}
          >
            Create credit note
          </button>
        )}
      </div>

      {snapshotQuery.isError && (
        <p className="text-sm text-rose-700" role="alert">
          {apiErrorMessage(snapshotQuery.error)}
        </p>
      )}
      {snapshotQuery.isFetching && (
        <p className="text-xs text-slate-500">Checking source-invoice eligibility…</p>
      )}
      {snapshotReady && allRemainingReserved && editingId === null && (
        <p className="text-xs text-slate-600 mb-2">
          All source quantities are already reserved by draft credit notes.
        </p>
      )}
      {draftsQuery.isLoading && (
        <p className="text-xs text-slate-500">Loading draft credit notes…</p>
      )}
      {draftsQuery.isError && (
        <p className="text-sm text-rose-700" role="alert">
          {apiErrorMessage(draftsQuery.error)}
        </p>
      )}

      {draftsQuery.data && draftsQuery.data.length === 0 && (
        <p className="text-xs text-slate-500">No draft credit notes for this source invoice.</p>
      )}
      {draftsQuery.data && draftsQuery.data.length > 0 && (
        <div className="overflow-auto">
          <table className="w-full text-xs min-w-[620px]">
            <thead className="text-left text-slate-500 border-b">
              <tr>
                <th className="py-2 pr-2">Credit-note number</th>
                <th className="py-2 pr-2">Issue date</th>
                <th className="py-2 pr-2">Status</th>
                <th className="py-2 pr-2 text-right">Subtotal</th>
                <th className="py-2 pr-2 text-right">GST</th>
                <th className="py-2 pr-2 text-right">Total</th>
                <th className="py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {draftsQuery.data.map((draft) => (
                <tr key={draft.id} className="border-b last:border-b-0">
                  <td className="py-2 pr-2 font-medium">{draft.credit_note_number}</td>
                  <td className="py-2 pr-2">{formatDate(draft.issue_date)}</td>
                  <td className="py-2 pr-2">
                    {draft.status === "authorised"
                      ? `authorised · ${Number(draft.applied_amount) > 0 ? "partially applied" : "unapplied"}`
                      : draft.status}
                  </td>
                  <td className="py-2 pr-2 text-right">{formatMoney(draft.subtotal, draft.currency)}</td>
                  <td className="py-2 pr-2 text-right">{formatMoney(draft.gst_amount, draft.currency)}</td>
                  <td className="py-2 pr-2 text-right font-medium">{formatMoney(draft.total, draft.currency)}</td>
                  <td className="py-2 whitespace-nowrap">
                    <button
                      type="button"
                      className="text-sky-700 hover:underline mr-3"
                      onClick={() => setEditingId(draft.id)}
                    >
                      {draft.status === "draft" ? "View/Edit" : "View"}
                    </button>
                    {draft.status === "draft" && (
                      <button
                        type="button"
                        className="text-rose-700 hover:underline"
                        onClick={() => setDeleteTarget(draft)}
                      >
                        Delete draft
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <CreditNoteDraftDialog
          sourceInvoiceId={invoiceId}
          onClose={() => setShowCreate(false)}
        />
      )}
      {editingId !== null && (
        <CreditNoteDraftDialog
          sourceInvoiceId={invoiceId}
          creditNoteId={editingId}
          onClose={() => setEditingId(null)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        destructive
        title="Delete this draft credit note?"
        message="Deleting removes only this draft. It does not change the source invoice, journal, invoice balance, payment allocation, or GST report."
        confirmLabel="Delete draft"
        busy={deleteMutation.isPending}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget.id);
        }}
      />
      {deleteMutation.isError && (
        <p className="mt-2 text-sm text-rose-700" role="alert">
          {apiErrorMessage(deleteMutation.error)}
        </p>
      )}
    </section>
  );
}
