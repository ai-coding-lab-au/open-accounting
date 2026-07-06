import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import type { Invoice, PdfUploadResult } from "../../types/api";
import InvoiceForm, {
  EMPTY_FORM,
  toCreatePayload,
  type InvoiceFormValues,
} from "./InvoiceForm";

async function uploadPdf(file: File): Promise<PdfUploadResult> {
  const fd = new FormData();
  fd.append("file", file);
  const { data } = await api.post<PdfUploadResult>("/invoices/upload-pdf", fd, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

async function createInvoice(payload: ReturnType<typeof toCreatePayload>): Promise<Invoice> {
  const { data } = await api.post<Invoice>("/invoices", payload);
  return data;
}

interface Props {
  onClose: () => void;
}

export default function ImportPdfDialog({ onClose }: Props) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PdfUploadResult | null>(null);
  const [form, setForm] = useState<InvoiceFormValues>(EMPTY_FORM);

  const uploadMut = useMutation({
    mutationFn: uploadPdf,
    onSuccess: (res) => {
      setPreview(res);
      setForm(EMPTY_FORM);
    },
  });

  const createMut = useMutation({
    mutationFn: createInvoice,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });

  const canUpload = !preview && !!file && !uploadMut.isPending;
  const canCreate =
    !!preview &&
    !createMut.isPending &&
    !!form.contact_name &&
    !!form.invoice_number &&
    !!form.issue_date &&
    !!form.total;

  const submit = () => {
    if (!preview) return;
    createMut.mutate(
      toCreatePayload(form, { source: "pdf", attachment_id: preview.attachment_id })
    );
  };

  useModalKeys({
    open: true,
    onClose,
    onSubmit: () => {
      if (canUpload && file) uploadMut.mutate(file);
      else if (canCreate) submit();
    },
  });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[760px] max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Attach PDF invoice</h2>
          <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="px-5 py-4 overflow-auto">
          {!preview && (
            <>
              <p className="text-sm text-slate-600 mb-3">
                Pick a PDF invoice. It will be saved locally and attached to the
                invoice you enter next.
              </p>
              <div className="border-2 border-dashed border-slate-300 rounded-lg p-6 text-center">
                <input
                  type="file"
                  accept=".pdf,application/pdf"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                />
                {file && (
                  <p className="text-xs text-slate-500 mt-2">
                    {file.name} · {(file.size / 1024).toFixed(1)} KB
                  </p>
                )}
              </div>
              {uploadMut.isError && (
                <p className="text-sm text-red-600 mt-3">{apiErrorMessage(uploadMut.error)}</p>
              )}
            </>
          )}

          {preview && (
            <>
              <div className="bg-slate-50 border border-slate-200 rounded p-3 mb-4 text-xs text-slate-700">
                <div className="flex items-center justify-between">
                  <div>
                    <span className="font-medium">{preview.filename}</span> ·{" "}
                    {(preview.size_bytes / 1024).toFixed(1)} KB
                  </div>
                </div>
              </div>
              <p className="text-sm text-slate-600 mb-3">
                Enter the invoice details. The uploaded PDF will be linked as the
                source attachment.
              </p>
              <InvoiceForm value={form} onChange={setForm} />
              {createMut.isError && (
                <p className="text-sm text-red-600 mt-3">{apiErrorMessage(createMut.error)}</p>
              )}
            </>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          {!preview && (
            <>
              <button className="btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                className="btn-primary"
                disabled={!canUpload}
                onClick={() => file && uploadMut.mutate(file)}
              >
                {uploadMut.isPending ? "Uploading..." : "Upload PDF"}
              </button>
            </>
          )}
          {preview && (
            <>
              <button
                className="btn-secondary"
                onClick={() => {
                  setPreview(null);
                  setFile(null);
                  setForm(EMPTY_FORM);
                }}
                disabled={createMut.isPending}
              >
                Pick a different file
              </button>
              <button
                className="btn-primary"
                onClick={submit}
                disabled={!canCreate}
              >
                {createMut.isPending ? "Saving…" : "Confirm & save"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
