import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { toast } from "../../lib/toast";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { useModalKeys } from "../../lib/useModalKeys";
import type { Invoice } from "../../types/api";
import { displayDocNumber, displayName, formatDate, formatMoney, statusBadgeClass } from "../../lib/format";

async function voidInvoice(id: number): Promise<void> {
  await api.delete(`/invoices/${id}`);
}

async function postInvoice(id: number): Promise<void> {
  await api.post(`/invoices/${id}/post`);
}

interface Props {
  invoice: Invoice;
  onClose: () => void;
}

export default function InvoiceDetailDrawer({ invoice, onClose }: Props) {
  const qc = useQueryClient();
  const [confirmVoid, setConfirmVoid] = useState(false);

  const voidMut = useMutation({
    mutationFn: () => voidInvoice(invoice.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });

  const authoriseMut = useMutation({
    mutationFn: () => postInvoice(invoice.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["journal"] });
      onClose();
    },
  });

  const isDraft = invoice.status === "draft";

  useModalKeys({ open: true, onClose });

  const att = invoice.attachments[0];
  // Attachment is fetched via Axios (which injects the full company identity)
  // into a blob URL —
  // an <iframe src=...> wouldn't send the header, so we use download/open-in-tab instead.

  return (
    <div className="fixed inset-0 z-40 flex">
      <div className="flex-1 bg-black/30" onClick={onClose} />
      <div className="w-[640px] bg-surface shadow-2xl flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <div className="text-xs text-slate-500">
              {invoice.direction === "AP" ? "Bill from supplier" : "Invoice to customer"} ·{" "}
              {invoice.source}
            </div>
            <h2 className="text-lg font-semibold">
              {displayDocNumber(invoice.invoice_number)}{" "}
              <span className={`ml-2 text-xs px-2 py-0.5 rounded ${statusBadgeClass(invoice.status)}`}>
                {invoice.status}
              </span>
            </h2>
          </div>
          <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="px-5 py-4 overflow-auto space-y-4 text-sm">
          <DetailGrid>
            <Row label="Contact">{displayName(invoice.contact_name, "provider")}</Row>
            <Row label="Issue date">{formatDate(invoice.issue_date)}</Row>
            <Row label="Due date">{formatDate(invoice.due_date)}</Row>
            <Row label="Currency">{invoice.currency}</Row>
            <Row label="Subtotal">{formatMoney(invoice.subtotal, invoice.currency)}</Row>
            <Row label="GST">{formatMoney(invoice.gst_amount, invoice.currency)}</Row>
            <Row label="Total">
              <span className="font-semibold">
                {formatMoney(invoice.total, invoice.currency)}
              </span>
            </Row>
            <Row label="Paid">{formatMoney(invoice.paid_amount, invoice.currency)}</Row>
          </DetailGrid>

          {invoice.notes && (
            <div>
              <div className="text-xs text-slate-500 mb-1">Notes</div>
              <div className="bg-slate-50 border border-slate-200 rounded p-2 whitespace-pre-wrap">
                {invoice.notes}
              </div>
            </div>
          )}

          {!isDraft && invoice.status !== "void" && (
            <div className="border border-slate-200 rounded p-3 bg-slate-50 text-xs text-slate-600">
              <div className="text-sm font-medium mb-1 text-slate-900">Recording payment</div>
              When the payment appears in the bank feed, categorise it to{" "}
              <b>Accounts Receivable (1100)</b> for customer invoices or{" "}
              <b>Accounts Payable (2000)</b> for supplier bills, keeping the
              standard tax code. Direct paid-amount edits are disabled until
              bank clearing links payments to invoices.
            </div>
          )}

          {att && (
            <div>
              <div className="text-xs text-slate-500 mb-1">Attachment</div>
              <div className="border border-slate-200 rounded p-2 flex items-center justify-between">
                <div>
                  <div className="font-medium">{att.filename}</div>
                  <div className="text-xs text-slate-500">
                    {att.mime_type} · {(att.size_bytes / 1024).toFixed(1)} KB
                  </div>
                </div>
                <AttachmentDownloadButton invoiceId={invoice.id} filename={att.filename} />
              </div>
            </div>
          )}

          {invoice.status !== "void" && (
            <div className="border-t border-slate-200 pt-3 flex items-center gap-4">
              {isDraft && (
                <button
                  className="btn-primary text-xs"
                  disabled={authoriseMut.isPending}
                  onClick={() => authoriseMut.mutate()}
                  title="Post this invoice to the general ledger and count it in AP/AR"
                >
                  {authoriseMut.isPending ? "Authorising…" : "Authorise (post to ledger)"}
                </button>
              )}
              <button
                className="text-xs text-rose-600 hover:underline"
                onClick={() => setConfirmVoid(true)}
              >
                {isDraft ? "Delete draft" : "Void this invoice"}
              </button>
            </div>
          )}

          {isDraft && (
            <p className="text-xs text-slate-500">
              This invoice is a <b>draft</b> — it isn't in the ledger or AP/AR
              totals yet. Click <b>Authorise</b> to post it.
            </p>
          )}

          {voidMut.isError && (
            <p className="text-sm text-red-600">{apiErrorMessage(voidMut.error)}</p>
          )}
          {authoriseMut.isError && (
            <p className="text-sm text-red-600">{apiErrorMessage(authoriseMut.error)}</p>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmVoid}
        destructive
        title={isDraft ? "Delete this draft?" : "Void this invoice?"}
        message={
          isDraft
            ? "This draft hasn't been posted, so it will be permanently deleted."
            : "It will be marked void and reversed out of the ledger / totals. This can't be undone from here."
        }
        confirmLabel={isDraft ? "Delete draft" : "Void"}
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

function DetailGrid({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-2 gap-x-4 gap-y-1">{children}</div>;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between border-b border-slate-100 py-1">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-900">{children}</span>
    </div>
  );
}

function AttachmentDownloadButton({
  invoiceId,
  filename,
}: {
  invoiceId: number;
  filename: string;
}) {
  const [busy, setBusy] = useState(false);

  const download = async () => {
    setBusy(true);
    try {
      const res = await api.get(`/invoices/${invoiceId}/attachment`, {
        responseType: "blob",
      });
      const url = window.URL.createObjectURL(res.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      toast(`Download failed: ${apiErrorMessage(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const openInTab = async () => {
    setBusy(true);
    try {
      const res = await api.get(`/invoices/${invoiceId}/attachment`, {
        responseType: "blob",
      });
      const url = window.URL.createObjectURL(res.data as Blob);
      window.open(url, "_blank", "noopener,noreferrer");
      // Don't revoke immediately; tab needs it.
    } catch (e) {
      toast(`Could not open attachment: ${apiErrorMessage(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex gap-2">
      <button className="btn-secondary text-xs" disabled={busy} onClick={openInTab}>
        Open
      </button>
      <button className="btn-secondary text-xs" disabled={busy} onClick={download}>
        Download
      </button>
    </div>
  );
}
