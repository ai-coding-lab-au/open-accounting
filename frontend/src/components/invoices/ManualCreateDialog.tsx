import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import type { Invoice, InvoiceDirection } from "../../types/api";
import InvoiceForm, {
  createEmptyInvoiceForm,
  toCreatePayload,
  validateInvoiceForm,
  type InvoiceFormValues,
} from "./InvoiceForm";
import { useCurrentCompany } from "../../lib/useCurrentCompany";

async function createInvoice(payload: ReturnType<typeof toCreatePayload>): Promise<Invoice> {
  const { data } = await api.post<Invoice>("/invoices", payload);
  return data;
}

export default function ManualCreateDialog({
  onClose,
  defaultDirection = "AP",
  showDirection = true,
}: {
  onClose: () => void;
  defaultDirection?: InvoiceDirection;
  showDirection?: boolean;
}) {
  const qc = useQueryClient();
  const companyQ = useCurrentCompany();
  const [form, setForm] = useState<InvoiceFormValues>(() =>
    createEmptyInvoiceForm(defaultDirection),
  );
  const [showValidation, setShowValidation] = useState(false);
  const mut = useMutation({
    mutationFn: createInvoice,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });
  const validationErrors = validateInvoiceForm(
    form,
    companyQ.data?.gst_registered ?? true,
  );
  const canCreate = !mut.isPending && !!companyQ.data && validationErrors.length === 0;
  const submit = () => {
    if (mut.isPending) return;
    const errors = validateInvoiceForm(form, companyQ.data?.gst_registered ?? true);
    if (errors.length) {
      setShowValidation(true);
      return;
    }
    if (!companyQ.data) return;
    mut.mutate(toCreatePayload(form, {
      source: "manual",
      gst_registered: companyQ.data.gst_registered,
    }));
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-surface rounded-lg shadow-xl w-full max-w-6xl max-h-[calc(100dvh-2rem)] flex flex-col">
        <div className="shrink-0 px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">New invoice</h2>
          <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="min-h-0 px-5 py-4 overflow-auto">
          <InvoiceForm value={form} onChange={setForm} showDirection={showDirection} />
          {showValidation && validationErrors.length > 0 && (
            <ul role="alert" className="mt-3 list-disc pl-5 text-sm text-red-700">
              {validationErrors.map((error) => <li key={error}>{error}</li>)}
            </ul>
          )}
          {mut.isError && (
            <p className="text-sm text-red-600 mt-3">
              {apiErrorMessage(mut.error)}
            </p>
          )}
        </div>
        <div className="shrink-0 px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={mut.isPending}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!canCreate}
            onClick={submit}
          >
            {mut.isPending ? "Saving…" : "Save Draft"}
          </button>
        </div>
      </div>
    </div>
  );
}
