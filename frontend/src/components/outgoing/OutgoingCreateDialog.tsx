import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import { useCompanyStore } from "../../store/company";
import type { Company, OutgoingDocType, OutgoingDocument } from "../../types/api";
import OutgoingEditor, {
  emptyEditorValues,
  toCreatePayload,
  type EditorValues,
} from "./OutgoingEditor";

export const DOC_TYPE_LABEL: Record<OutgoingDocType, string> = {
  receipt: "Receipt",
};

async function createOutgoing(payload: ReturnType<typeof toCreatePayload>) {
  const { data } = await api.post<OutgoingDocument>("/outgoing", payload);
  return data;
}

async function fetchCompany(id: string): Promise<Company> {
  const { data } = await api.get<Company>(`/companies/${id}`);
  return data;
}

export default function OutgoingCreateDialog({
  docType,
  onClose,
  onCreated,
}: {
  docType: OutgoingDocType;
  onClose: () => void;
  onCreated: (doc: OutgoingDocument) => void;
}) {
  const qc = useQueryClient();
  const currentId = useCompanyStore((s) => s.currentId);
  const { data: company } = useQuery({
    queryKey: ["company", currentId],
    queryFn: () => fetchCompany(currentId!),
    enabled: !!currentId,
  });
  const [values, setValues] = useState<EditorValues>(() => emptyEditorValues(docType, 28));
  const defaultPaymentTermsDays = company?.default_payment_terms_days ?? 28;

  const mut = useMutation({
    mutationFn: createOutgoing,
    onSuccess: (doc) => {
      qc.invalidateQueries({ queryKey: ["outgoing"] });
      qc.invalidateQueries({ queryKey: ["counters"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onCreated(doc);
    },
  });

  const missing =
    values.client_ref_id == null
      ? "Select a client"
      : values.issue_date.length === 0
        ? "Set an issue date"
        : !values.lines.some((l) => l.description.trim().length > 0)
          ? "Add at least one line item with a description"
          : null;
  const canSave = !mut.isPending && missing == null;
  const submit = () => {
    if (canSave) mut.mutate(toCreatePayload(values));
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[760px] max-h-[92vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">New {DOC_TYPE_LABEL[docType]}</h2>
          <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>
        {mut.isError && (
          <div className="mx-5 mt-3 text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded px-3 py-2">
            {apiErrorMessage(mut.error)}
          </div>
        )}
        <div className="px-5 py-4 overflow-auto">
          <OutgoingEditor
            value={values}
            onChange={setValues}
            defaultPaymentTermsDays={defaultPaymentTermsDays}
          />
        </div>
        <div className="px-5 py-3 border-t border-slate-200 flex items-center justify-end gap-2">
          {missing && !mut.isPending && (
            <span className="text-xs text-slate-500 mr-auto">{missing} to continue.</span>
          )}
          <button className="btn-secondary" onClick={onClose} disabled={mut.isPending}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={!canSave}
            onClick={submit}
          >
            {mut.isPending ? "Creating…" : "Create & preview PDF"}
          </button>
        </div>
      </div>
    </div>
  );
}
