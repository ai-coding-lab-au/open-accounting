import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import type { Invoice } from "../../types/api";
import InvoiceForm, {
  EMPTY_FORM,
  toCreatePayload,
  type InvoiceFormValues,
} from "./InvoiceForm";
import { useCurrentCompany } from "../../lib/useCurrentCompany";

async function createInvoice(payload: ReturnType<typeof toCreatePayload>): Promise<Invoice> {
  const { data } = await api.post<Invoice>("/invoices", payload);
  return data;
}

export default function ManualCreateDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const companyQ = useCurrentCompany();
  const [form, setForm] = useState<InvoiceFormValues>(EMPTY_FORM);
  const mut = useMutation({
    mutationFn: createInvoice,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });
  const canCreate =
    !mut.isPending &&
    !!companyQ.data &&
    !!form.contact_name &&
    !!form.invoice_number &&
    !!form.issue_date &&
    !!form.total;
  const submit = () => {
    if (canCreate) {
      mut.mutate(toCreatePayload(form, {
        source: "manual",
        gst_registered: companyQ.data!.gst_registered,
      }));
    }
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[680px] max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">New invoice</h2>
          <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="px-5 py-4 overflow-auto">
          <InvoiceForm value={form} onChange={setForm} />
          {mut.isError && (
            <p className="text-sm text-red-600 mt-3">
              {apiErrorMessage(mut.error)}
            </p>
          )}
        </div>
        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose} disabled={mut.isPending}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={!canCreate}
            onClick={submit}
          >
            {mut.isPending ? "Saving…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
