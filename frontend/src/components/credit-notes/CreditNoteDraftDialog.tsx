import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { formatDate, formatMoney } from "../../lib/format";
import { useModalKeys } from "../../lib/useModalKeys";
import { useCompanyStore } from "../../store/company";
import type {
  CreditNote,
  CreditNoteCreate,
  CreditNoteLineDraftIn,
  CreditNoteSource,
  CreditNoteUpdate,
  JournalEntry,
} from "../../types/api";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import CreditNoteApplications from "./CreditNoteApplications";

const QUANTITY_SCALE = 10000n;

function parseQuantity(value: string, allowZero = false): bigint | null {
  const match = /^(\d+)(?:\.(\d{1,4}))?$/.exec(value.trim());
  if (!match) return null;
  const whole = BigInt(match[1]);
  const fraction = BigInt((match[2] ?? "").padEnd(4, "0") || "0");
  const scaled = whole * QUANTITY_SCALE + fraction;
  return !allowZero && scaled === 0n ? null : scaled;
}


function formatQuantity(value: bigint): string {
  const whole = value / QUANTITY_SCALE;
  const fraction = (value % QUANTITY_SCALE)
    .toString()
    .padStart(4, "0")
    .replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole.toString();
}

function editableMaximum(
  lineId: number,
  snapshot: CreditNoteSource,
  draft: CreditNote | undefined,
): bigint | null {
  const sourceLine = snapshot.lines.find(
    (line) => line.source_invoice_line_id === lineId,
  );
  if (!sourceLine) return null;
  const remaining = parseQuantity(sourceLine.remaining_creditable_quantity, true);
  const existing = draft?.lines.find(
    (line) => line.source_invoice_line_id === lineId,
  );
  const existingQuantity = existing ? parseQuantity(existing.quantity) : 0n;
  if (remaining === null || existingQuantity === null) return null;
  return remaining + existingQuantity;
}

function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function sourceGstMode(snapshot: CreditNoteSource): string {
  if (snapshot.lines.every((line) => line.tax_code === "none")) return "No tax";
  return snapshot.gst_inclusive ? "Tax inclusive" : "Tax exclusive";
}

type SaveInput =
  | { mode: "create"; payload: CreditNoteCreate }
  | { mode: "edit"; payload: CreditNoteUpdate };

export default function CreditNoteDraftDialog({
  sourceInvoiceId,
  creditNoteId,
  onClose,
}: {
  sourceInvoiceId: number;
  creditNoteId?: number;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const currentId = useCompanyStore((state) => state.currentId);
  const [creditNoteNumber, setCreditNoteNumber] = useState("");
  const [issueDate, setIssueDate] = useState("");
  const [notes, setNotes] = useState("");
  const [quantities, setQuantities] = useState<Record<number, string>>({});
  const [savedDraft, setSavedDraft] = useState<CreditNote | null>(null);
  const [confirmAuthorise, setConfirmAuthorise] = useState(false);
  const [voidDate, setVoidDate] = useState("");
  const [confirmVoid, setConfirmVoid] = useState(false);
  const editing = creditNoteId !== undefined;

  const snapshotQuery = useQuery({
    queryKey: ["credit-notes", "source-snapshot", currentId, sourceInvoiceId],
    queryFn: async () =>
      (await api.get<CreditNoteSource>(`/credit-notes/source-invoices/${sourceInvoiceId}`)).data,
    enabled: !!currentId,
    retry: false,
    refetchOnMount: "always",
  });
  const detailQuery = useQuery({
    queryKey: ["credit-notes", "detail", currentId, creditNoteId],
    queryFn: async () =>
      (await api.get<CreditNote>(`/credit-notes/${creditNoteId}`)).data,
    enabled: !!currentId && editing,
    retry: false,
  });
  const draft = detailQuery.data;
  const displayedCreditNote = draft ?? savedDraft;
  const journalSourceType = displayedCreditNote?.direction === "AR"
    ? "credit_note_ar"
    : "credit_note_ap";
  const reversalJournalSourceType = displayedCreditNote?.direction === "AR"
    ? "credit_note_void_ar"
    : "credit_note_void_ap";
  const originalJournalQuery = useQuery({
    queryKey: ["journal", "credit-note", currentId, displayedCreditNote?.id],
    queryFn: async () => {
      const response = await api.get<JournalEntry[]>("/journal", {
        params: {
          source_type: journalSourceType,
          q: displayedCreditNote!.credit_note_number,
          limit: 500,
        },
      });
      return response.data.find((entry) => entry.source_id === displayedCreditNote?.id) ?? null;
    },
    enabled: !!currentId &&
      (displayedCreditNote?.status === "authorised" || displayedCreditNote?.status === "void"),
    retry: false,
  });
  const reversalJournalQuery = useQuery({
    queryKey: ["journal", "credit-note-void", currentId, displayedCreditNote?.id],
    queryFn: async () => {
      const response = await api.get<JournalEntry[]>("/journal", {
        params: {
          source_type: reversalJournalSourceType,
          q: displayedCreditNote!.credit_note_number,
          limit: 500,
        },
      });
      return response.data.find((entry) => entry.source_id === displayedCreditNote?.id) ?? null;
    },
    enabled: !!currentId && displayedCreditNote?.status === "void",
    retry: false,
  });

  useEffect(() => {
    const draft = detailQuery.data;
    if (!draft || !snapshotQuery.data) return;
    setCreditNoteNumber(draft.credit_note_number);
    setIssueDate(draft.issue_date);
    setNotes(draft.notes ?? "");
    const nextQuantities: Record<number, string> = {};
    for (const line of draft.lines) {
      const parsed = parseQuantity(line.quantity);
      nextQuantities[line.source_invoice_line_id] = parsed === null
        ? line.quantity
        : formatQuantity(parsed);
    }
    setQuantities(nextQuantities);
  }, [detailQuery.data, snapshotQuery.data]);

  useEffect(() => {
    if (detailQuery.data) setSavedDraft(detailQuery.data);
  }, [detailQuery.data]);

  const saveMutation = useMutation<CreditNote, unknown, SaveInput>({
    mutationFn: async (input) => {
      if (input.mode === "create") {
        return (await api.post<CreditNote>("/credit-notes", input.payload)).data;
      }
      return (await api.patch<CreditNote>(`/credit-notes/${creditNoteId}`, input.payload)).data;
    },
    onSuccess: (draft) => {
      setSavedDraft(draft);
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-list", currentId, sourceInvoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-snapshot", currentId, sourceInvoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "detail", currentId, draft.id],
        exact: true,
      });
    },
  });

  const authoriseMutation = useMutation({
    mutationFn: async () =>
      (await api.post<CreditNote>(`/credit-notes/${displayedCreditNote!.id}/post`)).data,
    onSuccess: (authorised) => {
      setSavedDraft(authorised);
      setConfirmAuthorise(false);
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-list", currentId, sourceInvoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-snapshot", currentId, sourceInvoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "detail", currentId, authorised.id],
        exact: true,
      });
      void queryClient.invalidateQueries({ queryKey: ["journal"] });
    },
  });

  const voidMutation = useMutation({
    mutationFn: async () =>
      (await api.post<CreditNote>(`/credit-notes/${displayedCreditNote!.id}/void`, {
        void_date: voidDate,
      })).data,
    onSuccess: (voided) => {
      setSavedDraft(voided);
      setConfirmVoid(false);
      setVoidDate("");
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-list", currentId, sourceInvoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "source-snapshot", currentId, sourceInvoiceId],
        exact: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["credit-notes", "detail", currentId, voided.id],
        exact: true,
      });
      void queryClient.invalidateQueries({ queryKey: ["journal"] });
      void queryClient.invalidateQueries({ queryKey: ["invoices", currentId] });
    },
  });

  const snapshot = snapshotQuery.data;
  const readOnly = !!displayedCreditNote && displayedCreditNote.status !== "draft";
  const isDraftNote = displayedCreditNote?.status === "draft";
  const isAuthorisedNote = displayedCreditNote?.status === "authorised";
  const hasActiveApplications = displayedCreditNote?.applications.some(
    (application) => application.status === "active",
  ) ?? false;
  const hasActiveRefunds = displayedCreditNote?.refunds.some(
    (refund) => refund.status === "active",
  ) ?? false;
  const voidDateError = !voidDate
    ? "Choose a void date."
    : !validDate(voidDate)
      ? "Enter a valid void date."
      : null;
  const voidBlocked = hasActiveApplications || hasActiveRefunds;
  const lineErrors: Record<number, string> = {};
  const payloadLines: CreditNoteLineDraftIn[] = [];
  let positiveLineCount = 0;

  for (const line of snapshot?.lines ?? []) {
    const rawQuantity = (quantities[line.source_invoice_line_id] ?? "").trim();
    if (!rawQuantity) continue;
    const parsedQuantity = parseQuantity(rawQuantity);
    if (parsedQuantity === null || parsedQuantity <= 0n) {
      lineErrors[line.source_invoice_line_id] = "Enter a positive quantity with up to four decimal places.";
      continue;
    }
    const maximum = editableMaximum(line.source_invoice_line_id, snapshot!, draft);
    if (maximum === null) {
      lineErrors[line.source_invoice_line_id] = "The available quantity could not be checked.";
      continue;
    }
    if (parsedQuantity > maximum) {
      lineErrors[line.source_invoice_line_id] = `Exceeds the maximum for this draft (${formatQuantity(maximum)}).`;
      continue;
    }
    payloadLines.push({
      source_invoice_line_id: line.source_invoice_line_id,
      quantity: formatQuantity(parsedQuantity),
    });
    positiveLineCount += 1;
  }

  const generalError = !creditNoteNumber.trim()
    ? "Enter a credit-note number."
    : !validDate(issueDate)
      ? "Enter a valid issue date."
      : positiveLineCount === 0
        ? "Enter a positive quantity for at least one source line."
        : Object.keys(lineErrors).length > 0
          ? "Correct the source-line quantities to continue."
          : null;
  const canSave =
    !!snapshot &&
    !snapshotQuery.isError &&
    !snapshotQuery.isFetching &&
    !readOnly &&
    (!editing || !!draft) &&
    !detailQuery.isError &&
    (editing || savedDraft === null) &&
    !saveMutation.isPending &&
    generalError === null;

  const submit = () => {
    if (!canSave) return;
    const common = {
      credit_note_number: creditNoteNumber.trim(),
      issue_date: issueDate,
      notes: notes.trim() || null,
      lines: payloadLines,
    };
    if (editing) {
      saveMutation.mutate({ mode: "edit", payload: common });
    } else {
      saveMutation.mutate({
        mode: "create",
        payload: { source_invoice_id: sourceInvoiceId, ...common },
      });
    }
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  const loading = snapshotQuery.isLoading || (editing && detailQuery.isLoading);
  const allRemainingReserved =
    !!snapshot &&
    snapshot.lines.length > 0 &&
    snapshot.lines.every((line) => {
      const remaining = parseQuantity(line.remaining_creditable_quantity, true);
      return remaining === 0n;
    });

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl border border-slate-200 w-[900px] max-w-full max-h-[94vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">
            {readOnly
              ? "View authorised credit note"
              : editing
                ? "Edit draft credit note"
                : "Create draft credit note"}
          </h2>
          <button type="button" className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>

        {snapshotQuery.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(snapshotQuery.error)}
          </p>
        )}
        {detailQuery.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(detailQuery.error)}
          </p>
        )}
        {saveMutation.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(saveMutation.error)}
          </p>
        )}
        {authoriseMutation.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(authoriseMutation.error)}
          </p>
        )}
        {voidMutation.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(voidMutation.error)}
          </p>
        )}
        {originalJournalQuery.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(originalJournalQuery.error)}
          </p>
        )}
        {reversalJournalQuery.isError && (
          <p className="mx-5 mt-3 text-sm text-rose-700" role="alert">
            {apiErrorMessage(reversalJournalQuery.error)}
          </p>
        )}

        {loading && <p className="px-5 py-4 text-sm text-slate-500">Loading source details…</p>}

        {snapshot && (!editing || draft) && (
          <div className="px-5 py-4 overflow-auto space-y-4 text-sm">
            <section aria-label="Locked source invoice details" className="border-b border-slate-200 pb-3">
              <h3 className="font-medium mb-2">Locked source invoice</h3>
              <div className="grid grid-cols-2 gap-x-5 gap-y-1">
                <LockedValue label="Source invoice number" value={snapshot.source_invoice_number} />
                <LockedValue label="Direction" value={snapshot.direction} />
                <LockedValue label="Contact" value={snapshot.contact_name} />
                <LockedValue label="Source issue date" value={formatDate(snapshot.issue_date)} />
                <LockedValue label="Currency" value={snapshot.currency} />
                <LockedValue label="GST mode" value={sourceGstMode(snapshot)} />
                <LockedValue label="Source subtotal" value={formatMoney(snapshot.subtotal, snapshot.currency)} />
                <LockedValue label="Source GST" value={formatMoney(snapshot.gst_amount, snapshot.currency)} />
                <LockedValue label="Source total" value={formatMoney(snapshot.total, snapshot.currency)} />
              </div>
            </section>

            <section aria-label="Credit-note identity">
              <h3 className="font-medium mb-2">{readOnly ? "Credit-note identity" : "Draft identity"}</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {readOnly ? (
                  <>
                    <LockedValue label="Credit-note number" value={displayedCreditNote!.credit_note_number} />
                    <LockedValue label="Issue date" value={formatDate(displayedCreditNote!.issue_date)} />
                    {displayedCreditNote!.notes && (
                      <div className="md:col-span-2">
                        <LockedValue label="Notes" value={displayedCreditNote!.notes} />
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <label className="block">
                      <span className="block text-xs text-slate-600 mb-1">Credit-note number</span>
                      <input
                        className="input w-full"
                        aria-label="Credit-note number"
                        maxLength={80}
                        value={creditNoteNumber}
                        onChange={(event) => setCreditNoteNumber(event.target.value)}
                      />
                    </label>
                    <label className="block">
                      <span className="block text-xs text-slate-600 mb-1">Issue date</span>
                      <input
                        className="input w-full"
                        type="date"
                        aria-label="Issue date"
                        value={issueDate}
                        onChange={(event) => setIssueDate(event.target.value)}
                      />
                    </label>
                    <label className="block md:col-span-2">
                      <span className="block text-xs text-slate-600 mb-1">Notes</span>
                      <textarea
                        className="input w-full min-h-20"
                        aria-label="Notes"
                        maxLength={1000}
                        value={notes}
                        onChange={(event) => setNotes(event.target.value)}
                      />
                    </label>
                  </>
                )}
              </div>
            </section>

            <section>
              <h3 className="font-medium mb-2">Source lines</h3>
              <div className="overflow-auto border border-slate-200 rounded">
                <table className="w-full text-xs min-w-[760px]">
                  <thead className="text-left text-slate-500 border-b bg-slate-50">
                    <tr>
                      <th className="p-2">Locked source details</th>
                      <th className="p-2 text-right">Reserved by draft credit notes</th>
                      <th className="p-2 text-right">Remaining creditable quantity</th>
                      <th className="p-2">Credited quantity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {snapshot.lines.map((line) => {
                      const maximum = readOnly
                        ? null
                        : editableMaximum(line.source_invoice_line_id, snapshot, draft);
                      const remaining = parseQuantity(line.remaining_creditable_quantity, true);
                      const inputDisabled = maximum === null || maximum === 0n;
                      const creditLine = displayedCreditNote?.lines.find(
                        (item) => item.source_invoice_line_id === line.source_invoice_line_id,
                      );
                      return (
                        <tr key={line.source_invoice_line_id} className="border-b last:border-b-0 align-top">
                          <td className="p-2 space-y-0.5">
                            <div className="font-medium text-slate-900">{line.description}</div>
                            <div>Account ID {line.account_id}</div>
                            <div>Unit price {formatMoney(line.unit_price, snapshot.currency)}</div>
                            <div>GST rate {line.gst_rate} · Tax code {line.tax_code}</div>
                            <div>Original source quantity {line.quantity}</div>
                            <div>Source line subtotal {formatMoney(line.line_subtotal, snapshot.currency)}</div>
                            <div>Source line GST {formatMoney(line.line_gst, snapshot.currency)}</div>
                            <div>Source line total {formatMoney(line.line_total, snapshot.currency)}</div>
                          </td>
                          <td className="p-2 text-right">{line.quantity_reserved}</td>
                          <td className="p-2 text-right">{line.remaining_creditable_quantity}</td>
                          <td className="p-2">
                            {readOnly ? (
                              <span>{creditLine?.quantity ?? "Not credited"}</span>
                            ) : (
                              <label className="block">
                                <span className="sr-only">Credited quantity for {line.description}</span>
                                <input
                                  className="input w-28"
                                  type="text"
                                  inputMode="decimal"
                                  aria-label={`Credited quantity for ${line.description}`}
                                  value={quantities[line.source_invoice_line_id] ?? ""}
                                  disabled={inputDisabled}
                                  onChange={(event) => setQuantities((previous) => ({
                                    ...previous,
                                    [line.source_invoice_line_id]: event.target.value,
                                  }))}
                                />
                              </label>
                            )}
                            {editing && maximum !== null && (
                              <div className="mt-1 text-slate-500">
                                Maximum for this draft: {formatQuantity(maximum)}
                              </div>
                            )}
                            {remaining === 0n && (
                              <div className="mt-1 text-slate-500">No new quantity is available.</div>
                            )}
                            {lineErrors[line.source_invoice_line_id] && (
                              <div className="mt-1 text-rose-700" role="alert">
                                {lineErrors[line.source_invoice_line_id]}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {!editing && allRemainingReserved && (
                <p className="mt-2 text-xs text-slate-600">
                  All source quantities are already reserved by draft credit notes.
                </p>
              )}
            </section>

            {displayedCreditNote && (
              <section className="border-t border-slate-200 pt-3" aria-live="polite">
                <h3 className="font-medium">
                  {displayedCreditNote.status === "draft" ? "Saved draft totals" : "Credit note totals"}
                </h3>
                <div className="flex flex-wrap gap-x-5 gap-y-1 mt-1">
                  <span>Subtotal {formatMoney(displayedCreditNote.subtotal, displayedCreditNote.currency)}</span>
                  <span>GST {formatMoney(displayedCreditNote.gst_amount, displayedCreditNote.currency)}</span>
                  <span className="font-medium">Total {formatMoney(displayedCreditNote.total, displayedCreditNote.currency)}</span>
                  <span>Status {displayedCreditNote.status}</span>
                </div>
              </section>
            )}
            {isDraftNote && (
              <p className="text-xs text-slate-500">
                A draft reserves source quantity only. It does not change the source invoice, journal, invoice balance, payment allocation, or GST report.
              </p>
            )}
            {(isAuthorisedNote || displayedCreditNote?.status === "void") && (
              <section className="border-t border-slate-200 pt-3 space-y-2" aria-label="Credit note journal entry">
                <div>
                  <h3 className="font-medium">Credit balances</h3>
                  <p className="text-xs text-slate-600">
                    Applied credit {formatMoney(displayedCreditNote!.applied_amount, displayedCreditNote!.currency)} ·
                    Remaining credit {formatMoney(displayedCreditNote!.remaining_amount, displayedCreditNote!.currency)}.
                    The credit has not been refunded or paid.
                  </p>
                </div>
                {originalJournalQuery.isLoading ? (
                  <p className="text-xs text-slate-500">Loading journal entry…</p>
                ) : originalJournalQuery.data ? (
                  <JournalEntryLines
                    journalEntry={originalJournalQuery.data}
                    currency={displayedCreditNote!.currency}
                    label="Original journal entry"
                  />
                ) : (
                  <p className="text-xs text-rose-700" role="status">No journal entry was found for this credit note.</p>
                )}
                {displayedCreditNote?.status === "void" && (
                  <div className="border-t border-slate-200 pt-2" aria-label="Reversal journal entry">
                    <h4 className="text-xs font-medium text-slate-700">Reversal journal</h4>
                    {reversalJournalQuery.isLoading ? (
                      <p className="text-xs text-slate-500">Loading reversal journal entry…</p>
                    ) : reversalJournalQuery.data ? (
                      <JournalEntryLines
                        journalEntry={reversalJournalQuery.data}
                        currency={displayedCreditNote.currency}
                        label="Reversal journal entry"
                      />
                    ) : (
                      <p className="text-xs text-rose-700" role="status">No reversal journal entry was found.</p>
                    )}
                  </div>
                )}
              </section>
            )}
            {isAuthorisedNote && (
              <section className="border-t border-slate-200 pt-3 space-y-2" aria-label="Void credit note">
              <div>
                <h3 className="font-medium">Void credit note</h3>
                <p className="text-xs text-slate-600">
                  The original ledger posting will be reversed and restoration is unavailable.
                </p>
              </div>
              <label className="block text-xs text-slate-600">
                <span className="block mb-1">Void date</span>
                <input
                  className="input w-full"
                  type="date"
                  aria-label="Void date"
                  value={voidDate}
                  onChange={(event) => setVoidDate(event.target.value)}
                  disabled={voidMutation.isPending}
                />
              </label>
              {voidBlocked && (
                <p className="text-xs text-rose-700" role="alert">
                  This credit note cannot be voided while it has active applications or refunds. Reverse all active applications and refunds first, then void the credit note.
                </p>
              )}
              {voidDateError && <p className="text-xs text-rose-700" role="alert">{voidDateError}</p>}
              <button
                type="button"
                className="btn-danger text-xs"
                disabled={voidBlocked || !!voidDateError || voidMutation.isPending}
                onClick={() => setConfirmVoid(true)}
              >
                {voidMutation.isPending ? "Voiding…" : "Void"}
              </button>
            </section>
            )}
            {isAuthorisedNote && <CreditNoteApplications creditNote={displayedCreditNote} />}
          </div>
        )}

        <div className="px-5 py-3 border-t border-slate-200 flex items-center justify-end gap-2">
          {!canSave && !loading && generalError && (
            <span className="text-xs text-slate-500 mr-auto" role="status">{generalError}</span>
          )}
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saveMutation.isPending}>
            Close
          </button>
          {isDraftNote && (
            <button
              type="button"
              className="btn-primary"
              disabled={authoriseMutation.isPending}
              onClick={() => setConfirmAuthorise(true)}
            >
              {authoriseMutation.isPending ? "Authorising…" : "Authorise"}
            </button>
          )}
          {!readOnly && (
            <button
              type="button"
              className="btn-primary"
              disabled={!canSave}
              onClick={submit}
            >
              {saveMutation.isPending
                ? "Saving draft…"
                : !editing && savedDraft
                  ? "Draft saved"
                  : editing
                    ? "Update draft"
                    : "Create draft"}
            </button>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={confirmAuthorise}
        title="Authorise this credit note?"
        message="Authorising posts this credit note to the ledger, makes it immutable, and leaves the credit available for approved invoice applications."
        confirmLabel="Authorise credit note"
        busy={authoriseMutation.isPending}
        onCancel={() => setConfirmAuthorise(false)}
        onConfirm={() => {
          if (isDraftNote) authoriseMutation.mutate();
        }}
      />
      <ConfirmDialog
        open={confirmVoid}
        destructive
        title="Void this credit note?"
        message="The original ledger posting will be reversed. Restoration is unavailable after the credit note is voided."
        confirmLabel="Void credit note"
        busy={voidMutation.isPending}
        onCancel={() => setConfirmVoid(false)}
        onConfirm={() => {
          if (isAuthorisedNote && !voidBlocked && !voidDateError) voidMutation.mutate();
        }}
      />
    </div>
  );
}

function JournalEntryLines({
  journalEntry,
  currency,
  label,
}: {
  journalEntry: JournalEntry;
  currency: string;
  label: string;
}) {
  return (
    <div>
      <div className="text-xs text-slate-600">
        {label} #{journalEntry.id} · {formatDate(journalEntry.entry_date)} · {journalEntry.memo}
        <div className="mt-1">Source type {journalEntry.source_type} · Source ID {journalEntry.source_id}</div>
        {journalEntry.reverses_entry_id !== null && (
          <div>Reverses entry ID {journalEntry.reverses_entry_id}</div>
        )}
      </div>
      <table className="w-full text-xs mt-2">
        <thead className="text-left text-slate-500 border-b">
          <tr>
            <th className="py-1 pr-2">Account ID</th>
            <th className="py-1 pr-2">Description</th>
            <th className="py-1 pr-2 text-right">Debit</th>
            <th className="py-1 text-right">Credit</th>
          </tr>
        </thead>
        <tbody>
          {journalEntry.lines.map((line) => (
            <tr key={line.id} className="border-b last:border-b-0">
              <td className="py-1 pr-2">{line.account_id}</td>
              <td className="py-1 pr-2">{line.description ?? ""}</td>
              <td className="py-1 pr-2 text-right">{formatMoney(line.debit_amount, currency)}</td>
              <td className="py-1 text-right">{formatMoney(line.credit_amount, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LockedValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2 border-b border-slate-100 py-1">
      <span className="text-slate-500">{label}</span>
      <span className="text-right text-slate-900">{value}</span>
    </div>
  );
}
