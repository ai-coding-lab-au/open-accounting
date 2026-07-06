import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import { useCompanyStore } from "../../store/company";
import type { OutgoingDocument } from "../../types/api";
import OutgoingEditor, {
  editorValuesFromDocument,
  toNotesOnlyPayload,
  toUpdatePayload,
  type EditorValues,
} from "./OutgoingEditor";
import { DOC_TYPE_LABEL } from "./OutgoingCreateDialog";

async function updateOutgoing(
  id: number,
  payload: ReturnType<typeof toUpdatePayload> | ReturnType<typeof toNotesOnlyPayload>,
) {
  const { data } = await api.patch<OutgoingDocument>(`/outgoing/${id}`, payload);
  return data;
}

export default function OutgoingEditDialog({
  doc,
  onClose,
  onSaved,
}: {
  doc: OutgoingDocument;
  onClose: () => void;
  onSaved: (doc: OutgoingDocument) => void;
}) {
  const qc = useQueryClient();
  const currentId = useCompanyStore((s) => s.currentId);
  const [values, setValues] = useState<EditorValues>(() => editorValuesFromDocument(doc));
  // Once issued, the backend locks every field except notes (void + re-create
  // to change payee/currency/amounts/date) — send a notes-only payload so a
  // notes-only edit doesn't trip that lock via the untouched `lines` field.
  const locked = doc.status !== "draft";

  const mut = useMutation({
    mutationFn: () =>
      updateOutgoing(doc.id, locked ? toNotesOnlyPayload(values) : toUpdatePayload(values)),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["outgoing"] });
      qc.invalidateQueries({ queryKey: ["outgoing-detail", currentId, doc.id] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onSaved(updated);
    },
  });

  const canSave =
    !mut.isPending &&
    (locked ||
      (values.client_ref_id != null &&
        values.issue_date.length > 0 &&
        values.lines.some((l) => l.description.trim().length > 0)));
  const submit = () => {
    if (canSave) mut.mutate();
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[760px] max-h-[92vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Edit {DOC_TYPE_LABEL[doc.doc_type]}</h2>
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
            showDocNumberOverride={false}
            locked={locked}
          />
        </div>
        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose} disabled={mut.isPending}>
            Cancel
          </button>
          <button className="btn-primary" disabled={!canSave} onClick={submit}>
            {mut.isPending ? "Saving..." : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
