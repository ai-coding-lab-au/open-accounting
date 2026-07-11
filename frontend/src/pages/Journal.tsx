import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatDate, formatMoney } from "../lib/format";
import { apiErrorMessage } from "../lib/errors";
import { useModalKeys } from "../lib/useModalKeys";
import { useCompanyStore } from "../store/company";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { DateInput } from "../components/ui/DateInput";
import { blockScientificNotation } from "../lib/numericInput";
import type {
  Account,
  JournalEntry,
  JournalEntryCreate,
  JournalEntryUpdate,
  JournalLineCreate,
} from "../types/api";

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function fetchEntries(): Promise<JournalEntry[]> {
  const { data } = await api.get<JournalEntry[]>("/journal", { params: { limit: 500 } });
  return data;
}

async function fetchAccounts(): Promise<Account[]> {
  const { data } = await api.get<Account[]>("/accounts");
  return data;
}

async function createEntry(
  payload: JournalEntryCreate,
  idempotencyKey: string,
): Promise<JournalEntry> {
  const { data } = await api.post<JournalEntry>("/journal", payload, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
  return data;
}

async function updateEntry(id: number, payload: JournalEntryUpdate): Promise<JournalEntry> {
  const { data } = await api.patch<JournalEntry>(`/journal/${id}`, payload);
  return data;
}

async function deleteEntry(id: number): Promise<void> {
  await api.delete(`/journal/${id}`);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sumDecimal(rows: { amount: string }[]): number {
  return rows.reduce((acc, r) => acc + (Number(r.amount) || 0), 0);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function JournalPage() {
  const currentId = useCompanyStore((s) => s.currentId);
  const qc = useQueryClient();

  const [editing, setEditing] = useState<JournalEntry | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<JournalEntry | null>(null);

  const entriesQ = useQuery({
    queryKey: ["journal", currentId],
    queryFn: fetchEntries,
    enabled: !!currentId,
  });

  const accountsQ = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: fetchAccounts,
    enabled: !!currentId,
  });

  const accountsById = useMemo(() => {
    const m = new Map<number, Account>();
    for (const a of accountsQ.data ?? []) m.set(a.id, a);
    return m;
  }, [accountsQ.data]);

  const remove = useMutation({
    mutationFn: (e: JournalEntry) => deleteEntry(e.id),
    onSuccess: () => {
      setDeleteError(null);
      qc.invalidateQueries({ queryKey: ["journal"] });
    },
    onError: (e) => setDeleteError(apiErrorMessage(e)),
  });

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">Pick a company in the top bar.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">Journal entries</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Manual postings for opening balances, period-end adjustments,
            depreciation, bad debt, and anything Invoice / Outgoing / Bank
            doesn't cover.
          </p>
        </div>
        <button
          onClick={() => setCreating(true)}
          className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800"
        >
          + New journal entry
        </button>
      </div>

      {deleteError && (
        <div className="rounded border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {deleteError}
        </div>
      )}

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        {entriesQ.isLoading || accountsQ.isLoading ? (
          <div className="px-4 py-6 text-sm text-slate-500">Loading…</div>
        ) : entriesQ.error ? (
          <div className="px-4 py-6 text-sm text-rose-700">
            {(entriesQ.error as Error).message}
          </div>
        ) : (entriesQ.data ?? []).length === 0 ? (
          <div className="px-4 py-10 text-center text-sm text-slate-500">
            No journal entries yet. Click "New journal entry" to add one.
          </div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
            <thead className="text-left text-slate-500 border-b bg-slate-50">
              <tr>
                <th className="py-2 px-3 w-28">Date</th>
                <th className="py-2 px-3">Memo</th>
                <th className="py-2 px-3 w-32">Reference</th>
                <th className="py-2 px-3 w-12 text-right">Lines</th>
                <th className="py-2 px-3 w-32 text-right">Total</th>
                <th className="py-2 px-3 w-36 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(entriesQ.data ?? []).map((e) => {
                const total = e.lines.reduce(
                  (acc, l) => acc + Number(l.debit_amount || 0),
                  0,
                );
                return (
                  <tr
                    key={e.id}
                    className="border-b last:border-b-0 hover:bg-slate-50"
                  >
                    <td className="py-1.5 px-3 font-mono">{formatDate(e.entry_date)}</td>
                    <td className="py-1.5 px-3">
                      <div>{e.memo}</div>
                      <details className="text-xs text-slate-500 mt-0.5">
                        <summary className="cursor-pointer hover:text-slate-700">
                          show lines
                        </summary>
                        <table className="mt-1 ml-2">
                          <tbody>
                            {e.lines.map((l) => {
                              const acc = accountsById.get(l.account_id);
                              const accLabel = acc
                                ? `${acc.code} — ${acc.name}`
                                : `#${l.account_id}`;
                              const d = Number(l.debit_amount);
                              const c = Number(l.credit_amount);
                              return (
                                <tr key={l.id}>
                                  <td className="pr-3 py-0.5">{accLabel}</td>
                                  <td className="pr-3 py-0.5 text-right tabular-nums">
                                    {d > 0 ? formatMoney(l.debit_amount) : ""}
                                  </td>
                                  <td className="pr-3 py-0.5 text-right tabular-nums">
                                    {c > 0 ? formatMoney(l.credit_amount) : ""}
                                  </td>
                                  <td className="text-slate-400 italic">
                                    {l.description ?? ""}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </details>
                    </td>
                    <td className="py-1.5 px-3 text-xs">{e.reference ?? ""}</td>
                    <td className="py-1.5 px-3 text-right tabular-nums">
                      {e.lines.length}
                    </td>
                    <td className="py-1.5 px-3 text-right tabular-nums">
                      {formatMoney(total)}
                    </td>
                    <td className="py-1.5 px-3 text-right space-x-2">
                      <button
                        onClick={() => setEditing(e)}
                        className="text-xs text-slate-600 hover:text-slate-900 underline"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => {
                          setDeleteError(null);
                          setPendingDelete(e);
                        }}
                        disabled={remove.isPending}
                        className="text-xs text-rose-600 hover:text-rose-800 underline"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
      </div>

      {(creating || editing) && accountsQ.data && (
        <JournalEntryDialog
          key={editing ? `edit-${editing.id}` : "create"}
          existing={editing}
          accounts={accountsQ.data}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["journal"] });
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        destructive
        title="Delete journal entry?"
        message={
          pendingDelete
            ? `Delete journal entry from ${formatDate(pendingDelete.entry_date)}? ${pendingDelete.memo}`
            : ""
        }
        confirmLabel="Delete"
        busy={remove.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          setDeleteError(null);
          remove.mutate(pendingDelete, {
            onSettled: () => setPendingDelete(null),
          });
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create/edit dialog
// ---------------------------------------------------------------------------

type DraftLine = {
  // Local-only key so we don't lose focus when adding/removing rows.
  key: string;
  account_id: number | "";
  amount: string;       // single editable field; side decides debit vs credit
  side: "debit" | "credit";
  description: string;
};

function emptyLine(side: "debit" | "credit"): DraftLine {
  return {
    key: Math.random().toString(36).slice(2),
    account_id: "",
    amount: "",
    side,
    description: "",
  };
}

function linesFromEntry(entry: JournalEntry): DraftLine[] {
  return entry.lines.map((l) => {
    const side: "debit" | "credit" =
      Number(l.debit_amount) > 0 ? "debit" : "credit";
    return {
      key: String(l.id),
      account_id: l.account_id,
      amount: side === "debit" ? l.debit_amount : l.credit_amount,
      side,
      description: l.description ?? "",
    };
  });
}

function JournalEntryDialog({
  existing,
  accounts,
  onClose,
  onSaved,
}: {
  existing: JournalEntry | null;
  accounts: Account[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = !!existing;
  const [entryDate, setEntryDate] = useState(existing?.entry_date ?? todayIso());
  const [memo, setMemo] = useState(existing?.memo ?? "");
  const [reference, setReference] = useState(existing?.reference ?? "");
  const [lines, setLines] = useState<DraftLine[]>(
    existing
      ? linesFromEntry(existing)
      : [emptyLine("debit"), emptyLine("credit")],
  );
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Keep one operation key for the lifetime of this create editor. If the
  // local request times out after the backend committed, pressing Save again
  // replays the same operation instead of writing a duplicate journal entry.
  const createIdempotencyKey = useRef(crypto.randomUUID());

  const activeAccounts = useMemo(
    () => accounts.filter((a) => a.active).sort((a, b) => a.code.localeCompare(b.code)),
    [accounts],
  );

  const debitTotal = sumDecimal(
    lines.filter((l) => l.side === "debit").map((l) => ({ amount: l.amount })),
  );
  const creditTotal = sumDecimal(
    lines.filter((l) => l.side === "credit").map((l) => ({ amount: l.amount })),
  );
  const diff = debitTotal - creditTotal;
  // Every line must carry a positive amount. A negative line can make the naive
  // debit/credit sums net to zero ("Balanced ✓") while submission is blocked —
  // so the indicator must also require all amounts > 0.
  const allLinesPositive = lines.every((l) => {
    const n = Number(l.amount);
    return Number.isFinite(n) && n > 0;
  });
  const balanced = Math.abs(diff) < 0.005 && allLinesPositive;

  function patchLine(key: string, patch: Partial<DraftLine>) {
    setLines((curr) => curr.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  }
  function addLine(side: "debit" | "credit") {
    setLines((curr) => [...curr, emptyLine(side)]);
  }
  function removeLine(key: string) {
    setLines((curr) => (curr.length <= 2 ? curr : curr.filter((l) => l.key !== key)));
  }

  const save = useMutation({
    mutationFn: async () => {
      const payloadLines: JournalLineCreate[] = lines.map((l) => ({
        account_id: l.account_id as number,
        debit_amount: l.side === "debit" ? l.amount : "0",
        credit_amount: l.side === "credit" ? l.amount : "0",
        description: l.description.trim() || null,
      }));

      if (isEdit) {
        const payload: JournalEntryUpdate = {
          entry_date: entryDate,
          memo: memo.trim(),
          reference: reference.trim() || null,
          lines: payloadLines,
        };
        return updateEntry(existing!.id, payload);
      } else {
        const payload: JournalEntryCreate = {
          entry_date: entryDate,
          memo: memo.trim(),
          reference: reference.trim() || null,
          lines: payloadLines,
        };
        return createEntry(payload, createIdempotencyKey.current);
      }
    },
    onSuccess: () => onSaved(),
    onError: (e) => {
      setSubmitError(apiErrorMessage(e));
    },
  });

  useModalKeys({ open: true, onClose, onSubmit: onSave });

  function onSave() {
    setSubmitError(null);
    if (!entryDate) {
      setSubmitError("Enter a valid date (DD/MM/YYYY).");
      return;
    }
    if (!memo.trim()) {
      setSubmitError("Memo is required.");
      return;
    }
    if (lines.length < 2) {
      setSubmitError("At least two lines are required.");
      return;
    }
    for (const [idx, l] of lines.entries()) {
      if (l.account_id === "") {
        setSubmitError(`Line ${idx + 1}: pick an account.`);
        return;
      }
      // Reject scientific notation / stray characters: only plain decimals.
      if (!/^\d*\.?\d+$/.test(l.amount.trim())) {
        setSubmitError(`Line ${idx + 1}: enter a plain amount like 1234.56.`);
        return;
      }
      const n = Number(l.amount);
      if (!Number.isFinite(n) || n <= 0) {
        setSubmitError(`Line ${idx + 1}: amount must be a positive number.`);
        return;
      }
    }
    if (!balanced) {
      setSubmitError(
        `Entry is unbalanced: debit ${debitTotal.toFixed(2)} vs credit ${creditTotal.toFixed(
          2,
        )} (diff ${diff.toFixed(2)}).`,
      );
      return;
    }
    save.mutate();
  }

  return (
    <div className="fixed inset-0 z-40 bg-slate-900/30 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-full max-w-3xl max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b flex justify-between items-center">
          <h3 className="font-semibold">
            {isEdit ? `Edit journal entry #${existing!.id}` : "New journal entry"}
          </h3>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-900">
            ✕
          </button>
        </div>

        {submitError && (
          <div className="mx-5 mt-3 text-rose-700 text-xs bg-rose-50 border border-rose-200 rounded px-3 py-2">
            {submitError}
          </div>
        )}

        <div className="p-5 space-y-3 text-sm overflow-y-auto flex-1">
          <div className="grid grid-cols-3 gap-3">
            <Field label="Date" hint="(DD/MM/YYYY)">
              <DateInput
                value={entryDate}
                onChange={setEntryDate}
                className="border rounded px-2 py-1 w-full pr-7"
              />
            </Field>
            <Field label="Reference (optional)">
              <input
                value={reference}
                onChange={(e) => setReference(e.target.value)}
                className="border rounded px-2 py-1 w-full"
                placeholder="e.g. cheque #, doc ref"
              />
            </Field>
            <Field label="Memo">
              <input
                value={memo}
                onChange={(e) => {
                  setMemo(e.target.value);
                  // Clear the "Memo is required." error as soon as the user
                  // starts typing, so a stale validation message can't linger.
                  if (submitError === "Memo is required." && e.target.value.trim()) {
                    setSubmitError(null);
                  }
                }}
                className="border rounded px-2 py-1 w-full"
                placeholder="e.g. Owner contribution opening"
              />
            </Field>
          </div>

          <div className="border rounded">
            <table className="w-full text-sm">
              <thead className="text-left text-slate-500 bg-slate-50 border-b">
                <tr>
                  <th className="py-1.5 px-2 w-64">Account</th>
                  <th className="py-1.5 px-2 w-24">Side</th>
                  <th className="py-1.5 px-2 w-32 text-right">Amount</th>
                  <th className="py-1.5 px-2">Description (optional)</th>
                  <th className="py-1.5 px-2 w-8"></th>
                </tr>
              </thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.key} className="border-b last:border-b-0">
                    <td className="py-1 px-2">
                      <select
                        value={l.account_id === "" ? "" : String(l.account_id)}
                        onChange={(e) =>
                          patchLine(l.key, {
                            account_id:
                              e.target.value === "" ? "" : Number(e.target.value),
                          })
                        }
                        className="border rounded px-1 py-0.5 w-full text-xs"
                      >
                        <option value="">— pick account —</option>
                        {activeAccounts.map((a) => (
                          <option key={a.id} value={a.id}>
                            {a.code} — {a.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="py-1 px-2">
                      <select
                        value={l.side}
                        onChange={(e) =>
                          patchLine(l.key, {
                            side: e.target.value as "debit" | "credit",
                          })
                        }
                        className="border rounded px-1 py-0.5 w-full text-xs"
                      >
                        <option value="debit">Debit</option>
                        <option value="credit">Credit</option>
                      </select>
                    </td>
                    <td className="py-1 px-2">
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        value={l.amount}
                        onChange={(e) => patchLine(l.key, { amount: e.target.value })}
                        onKeyDown={blockScientificNotation}
                        className="border rounded px-1 py-0.5 w-full text-right tabular-nums text-xs"
                        placeholder="0.00"
                      />
                    </td>
                    <td className="py-1 px-2">
                      <input
                        value={l.description}
                        onChange={(e) =>
                          patchLine(l.key, { description: e.target.value })
                        }
                        className="border rounded px-1 py-0.5 w-full text-xs"
                      />
                    </td>
                    <td className="py-1 px-2 text-right">
                      <button
                        onClick={() => removeLine(l.key)}
                        disabled={lines.length <= 2}
                        className="text-rose-500 hover:text-rose-700 disabled:opacity-30"
                        title="Remove line"
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="bg-slate-50 border-t">
                <tr>
                  <td colSpan={2} className="py-1.5 px-2 text-xs text-slate-500">
                    <button
                      onClick={() => addLine("debit")}
                      className="text-slate-600 hover:text-slate-900 underline mr-3"
                    >
                      + Debit
                    </button>
                    <button
                      onClick={() => addLine("credit")}
                      className="text-slate-600 hover:text-slate-900 underline"
                    >
                      + Credit
                    </button>
                  </td>
                  <td className="py-1.5 px-2 text-right tabular-nums text-xs">
                    <div>Dr {formatMoney(debitTotal)}</div>
                    <div>Cr {formatMoney(creditTotal)}</div>
                  </td>
                  <td className="py-1.5 px-2 text-xs">
                    {balanced ? (
                      <span className="text-emerald-700">Balanced ✓</span>
                    ) : !allLinesPositive ? (
                      <span className="text-rose-700">Each amount must be &gt; 0</span>
                    ) : (
                      <span className="text-rose-700">
                        Diff {formatMoney(diff)}
                      </span>
                    )}
                  </td>
                  <td></td>
                </tr>
              </tfoot>
            </table>
          </div>

        </div>

        <div className="px-5 py-3 border-t flex justify-end gap-2 bg-slate-50">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-sm rounded border hover:bg-surface"
          >
            Cancel
          </button>
          <button
            onClick={onSave}
            disabled={save.isPending}
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {save.isPending ? "Saving…" : isEdit ? "Save changes" : "Create entry"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs text-slate-500 block mb-0.5">
        {label} {hint && <span className="text-slate-400 font-normal">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
