import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useCompanyStore } from "../store/company";
import { displayId, displayName, formatDate, formatMoney } from "../lib/format";
import { apiErrorMessage } from "../lib/errors";
import { useModalKeys } from "../lib/useModalKeys";
import { todayLocal } from "../lib/date";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { DateInput } from "../components/ui/DateInput";
import { blockScientificNotation } from "../lib/numericInput";
import type {
  Account,
  BankAccountWithBalance,
  BankImportCommitResult,
  BankImportPreview,
  BankImportPreviewRow,
  BankTransaction,
  BankTransactionIn,
  TaxCode,
} from "../types/api";

async function fetchBankAccounts(): Promise<BankAccountWithBalance[]> {
  const { data } = await api.get<BankAccountWithBalance[]>("/bank-accounts");
  return data;
}

async function fetchTransactions(
  accountId: number,
): Promise<BankTransaction[]> {
  const { data } = await api.get<BankTransaction[]>(
    `/bank-accounts/${accountId}/transactions`,
  );
  return data;
}

async function fetchAccounts(): Promise<Account[]> {
  const { data } = await api.get<Account[]>("/accounts");
  return data;
}

async function createTransaction(
  accountId: number,
  payload: BankTransactionIn,
): Promise<BankTransaction> {
  const { data } = await api.post<BankTransaction>(
    `/bank-accounts/${accountId}/transactions`,
    payload,
  );
  return data;
}

async function deleteTransaction(txnId: number): Promise<void> {
  await api.delete(`/bank-accounts/transactions/${txnId}`);
}

async function updateBankAccount(
  accountId: number,
  payload: { name?: string; bsb?: string | null; account_number?: string | null },
): Promise<BankAccountWithBalance> {
  const { data } = await api.patch<BankAccountWithBalance>(
    `/bank-accounts/${accountId}`,
    payload,
  );
  return data;
}

type Props = {
  title: string;
  blurb: string;
};

export default function BankAccountPage({ title, blurb }: Props) {
  const currentId = useCompanyStore((s) => s.currentId);
  const qc = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<BankTransaction | null>(null);

  const { data: accounts, isLoading } = useQuery({
    queryKey: ["bank-accounts", currentId],
    queryFn: fetchBankAccounts,
    enabled: !!currentId,
  });

  const account = (accounts ?? [])[0] ?? null;

  const { data: txns, isLoading: loadingTxns } = useQuery({
    queryKey: ["bank-transactions", currentId, account?.id],
    queryFn: () => fetchTransactions(account!.id),
    enabled: !!account,
  });

  const { data: coa } = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: fetchAccounts,
    enabled: !!currentId,
  });

  const accountsById = useMemo(() => {
    const m = new Map<number, Account>();
    (coa ?? []).forEach((a) => m.set(a.id, a));
    return m;
  }, [coa]);

  const delMut = useMutation({
    mutationFn: deleteTransaction,
    onSuccess: () => {
      setDeleteError(null);
      qc.invalidateQueries({ queryKey: ["bank-transactions", currentId, account?.id] });
      qc.invalidateQueries({ queryKey: ["bank-accounts", currentId] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (e) => setDeleteError(apiErrorMessage(e)),
  });

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">
          Pick a company in the top bar.
        </p>
      </div>
    );
  }

  if (isLoading) {
    return <div className="text-sm text-slate-500">Loading…</div>;
  }

  if (!account) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6">
        <h2 className="font-semibold">No bank account configured</h2>
        <p className="text-sm text-slate-500 mt-1">
          A default account should exist for every company. Restart the backend,
          or contact support if this persists.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">{title}</h1>
          <p className="text-xs text-slate-500 mt-0.5">{blurb}</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex gap-2">
            <button className="btn-secondary" onClick={() => setShowImport(true)}>
              Import statement…
            </button>
            <button className="btn-primary" onClick={() => setShowNew(true)}>
              + New transaction
            </button>
          </div>
        </div>
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 p-5">
        <div className="flex items-end justify-between flex-wrap gap-3">
          <div>
            <div className="flex items-center gap-2">
              <div className="text-xs uppercase tracking-wide text-slate-500">
                {displayName(account.name, "company")}
              </div>
              <button
                className="text-xs text-slate-500 hover:text-slate-900 underline"
                onClick={() => setShowEdit(true)}
              >
                Edit
              </button>
            </div>
            <div
              className={`mt-1 text-4xl font-semibold ${
                Number(account.current_balance) < 0
                  ? "text-rose-700"
                  : "text-slate-900"
              }`}
            >
              {formatMoney(account.current_balance)}
            </div>
            <div className="text-xs text-slate-500 mt-1">
              {account.bsb && account.account_number
                ? `BSB ${displayId(account.bsb, "bsb")} · Acc ${displayId(account.account_number, "account")}`
                : "Bank details not set — click Edit to add them"}
            </div>
          </div>
          <div className="text-right text-xs text-slate-500">
            <div>Opening balance: {formatMoney(account.opening_balance)}</div>
            <div>{txns?.length ?? 0} transactions on record</div>
          </div>
        </div>
        {Number(account.current_balance) < 0 && (
          <div className="mt-4 text-xs text-rose-800 bg-rose-50 border border-rose-200 rounded px-3 py-2">
            This account balance is negative. Check for unrecorded deposits, a
            wrong opening balance, or transactions entered with the wrong
            direction.
          </div>
        )}
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        <div className="px-4 py-3 border-b bg-slate-50 text-sm font-semibold">
          Transaction history
        </div>
        {deleteError && (
          <div className="border-b border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
            {deleteError}
          </div>
        )}
        {loadingTxns ? (
          <div className="px-4 py-6 text-sm text-slate-500">Loading…</div>
        ) : !txns || txns.length === 0 ? (
          <div className="px-4 py-6 text-sm text-slate-500">
            No transactions yet.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead className="text-xs text-slate-600 bg-slate-50">
                <tr>
                  <th className="text-left px-4 py-2">Date</th>
                  <th className="text-left px-4 py-2">Counter-party</th>
                  <th className="text-left px-4 py-2">Memo</th>
                  <th className="text-left px-4 py-2">Category</th>
                  <th className="text-right px-4 py-2">In</th>
                  <th className="text-right px-4 py-2">Out</th>
                  <th className="sticky right-0 z-10 w-10 bg-slate-50 px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {txns.map((t) => {
                  const cat = t.account_id ? accountsById.get(t.account_id) : null;
                  return (
                    <tr key={t.id} className="border-t">
                      <td className="px-4 py-2 text-xs whitespace-nowrap">
                        {formatDate(t.occurred_at)}
                      </td>
                      <td className="px-4 py-2 text-xs">
                        {displayName(t.counter_party_name, "provider")}
                      </td>
                      <td className="px-4 py-2 text-xs text-slate-600">
                        {t.memo ?? "—"}
                      </td>
                      <td className="px-4 py-2 text-xs">
                        {cat ? (
                          <span className="text-slate-700">
                            <span className="font-mono text-slate-500">
                              {cat.code}
                            </span>{" "}
                            {cat.name}
                          </span>
                        ) : (
                          <span className="text-amber-700">Uncategorised</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right text-emerald-700">
                        {t.direction === "in" ? formatMoney(t.amount) : ""}
                      </td>
                      <td className="px-4 py-2 text-right text-rose-700">
                        {t.direction === "out" ? formatMoney(t.amount) : ""}
                      </td>
                      <td className="sticky right-0 bg-surface px-2 py-2 text-right">
                        <button
                          className="inline-flex h-7 w-7 items-center justify-center rounded border border-transparent text-base leading-none text-rose-600 hover:border-rose-200 hover:bg-rose-50 hover:text-rose-800"
                          onClick={() => {
                            setDeleteError(null);
                            setPendingDelete(t);
                          }}
                          aria-label="Delete transaction"
                          title="Delete transaction"
                        >
                          ×
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

      {showNew && account && (
        <NewTransactionDialog
          bankAccountId={account.id}
          accounts={coa ?? []}
          onClose={() => setShowNew(false)}
          onSaved={() => {
            qc.invalidateQueries({
              queryKey: ["bank-transactions", currentId, account.id],
            });
            qc.invalidateQueries({ queryKey: ["bank-accounts", currentId] });
            qc.invalidateQueries({ queryKey: ["dashboard"] });
            setShowNew(false);
          }}
        />
      )}

      {showImport && account && (
        <ImportStatementDialog
          bankAccountId={account.id}
          accounts={coa ?? []}
          onClose={() => setShowImport(false)}
          onImported={() => {
            qc.invalidateQueries({
              queryKey: ["bank-transactions", currentId, account.id],
            });
            qc.invalidateQueries({ queryKey: ["bank-accounts", currentId] });
            qc.invalidateQueries({ queryKey: ["dashboard"] });
          }}
          onCommitted={() => {
            setShowImport(false);
          }}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        destructive
        title="Delete transaction?"
        message={
          pendingDelete
            ? `Delete ${pendingDelete.direction === "in" ? "deposit" : "withdrawal"} of ${formatMoney(pendingDelete.amount)} on ${formatDate(pendingDelete.occurred_at)}?`
            : ""
        }
        confirmLabel="Delete"
        busy={delMut.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          setDeleteError(null);
          delMut.mutate(pendingDelete.id, {
            onSettled: () => setPendingDelete(null),
          });
        }}
      />

      {showEdit && account && (
        <EditAccountDialog
          account={account}
          onClose={() => setShowEdit(false)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["bank-accounts", currentId] });
            setShowEdit(false);
          }}
        />
      )}
    </div>
  );
}

function EditAccountDialog({
  account,
  onClose,
  onSaved,
}: {
  account: BankAccountWithBalance;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(account.name);
  const [bsb, setBsb] = useState(account.bsb ?? "");
  const [accountNumber, setAccountNumber] = useState(account.account_number ?? "");

  const mut = useMutation({
    mutationFn: () =>
      updateBankAccount(account.id, {
        name: name.trim(),
        bsb: bsb.trim() || null,
        account_number: accountNumber.trim() || null,
      }),
    onSuccess: () => onSaved(),
  });

  const canSave = name.trim().length > 0 && !mut.isPending;
  const submit = () => {
    if (canSave) mut.mutate();
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[440px]">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Edit account</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-900">
            ×
          </button>
        </div>
        <div className="px-5 py-4 space-y-3">
          <label className="block text-sm">
            <span className="block text-slate-600 mb-1">Account name</span>
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-sm">
              <span className="block text-slate-600 mb-1">BSB</span>
              <input
                className="input"
                value={bsb}
                onChange={(e) => setBsb(e.target.value)}
                placeholder="000-000"
              />
            </label>
            <label className="block text-sm">
              <span className="block text-slate-600 mb-1">Account number</span>
              <input
                className="input"
                value={accountNumber}
                onChange={(e) => setAccountNumber(e.target.value)}
                placeholder="11939615"
              />
            </label>
          </div>
          {mut.isError && (
            <p className="text-sm text-rose-600">{apiErrorMessage(mut.error)}</p>
          )}
        </div>
        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose} disabled={mut.isPending}>
            Cancel
          </button>
          <button className="btn-primary" disabled={!canSave} onClick={submit}>
            {mut.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function NewTransactionDialog({
  bankAccountId,
  accounts,
  onClose,
  onSaved,
}: {
  bankAccountId: number;
  accounts: Account[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const today = todayLocal();
  const [direction, setDirection] = useState<"in" | "out">("out");
  const [amount, setAmount] = useState("");
  const [occurredAt, setOccurredAt] = useState(today);
  const [counterParty, setCounterParty] = useState("");
  const [memo, setMemo] = useState("");
  const [accountId, setAccountId] = useState<number | "">("");
  const [showOtherAccounts, setShowOtherAccounts] = useState(false);
  const [gstAmount, setGstAmount] = useState("0");
  const [gstTouched, setGstTouched] = useState(false);
  const [taxCode, setTaxCode] = useState<
    "standard" | "gst_free" | "input_taxed" | "capital" | "none"
  >("standard");

  const noGstCodes = new Set(["gst_free", "input_taxed", "none"]);
  const gstBearing = !noGstCodes.has(taxCode);
  // Standard / capital movements are GST-inclusive by convention: default the
  // GST portion to amount ÷ 11 (matching the reconciliation + import paths) so a
  // manually-entered sale/purchase isn't silently recorded with $0 GST — which
  // is what made a standard-rated sale land in BAS G6 with 1A = 0. The user can
  // still override the field.
  const autoGst =
    gstBearing && Number(amount) > 0 ? (Number(amount) / 11).toFixed(2) : "0";
  const effectiveGst = !gstBearing ? "0" : gstTouched ? gstAmount || "0" : autoGst;

  const mut = useMutation({
    mutationFn: () =>
      createTransaction(bankAccountId, {
        direction,
        amount,
        occurred_at: occurredAt,
        counter_party_name: counterParty || null,
        memo: memo || null,
        account_id: accountId === "" ? null : accountId,
        gst_amount: effectiveGst,
        tax_code: taxCode,
      }),
    onSuccess: () => onSaved(),
  });

  const validAmt = Number(amount) > 0;
  const validGst =
    Number(effectiveGst) >= 0 && Number(effectiveGst) <= Number(amount || 0);
  const canSave = validAmt && validGst && occurredAt && !mut.isPending;
  const submit = () => {
    if (canSave) mut.mutate();
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  // Filter accounts so the relevant types come first based on direction.
  const eligible = useMemo(() => {
    const inTypes = new Set(["INCOME"]);
    const outTypes = new Set(["EXPENSE", "COST_OF_SALES"]);
    const others = ["ASSET", "LIABILITY", "EQUITY"];
    const primaryTypes = direction === "in" ? inTypes : outTypes;
    const primary = accounts.filter((a) => a.active && primaryTypes.has(a.type));
    const rest = accounts.filter(
      (a) =>
        a.active && !primaryTypes.has(a.type) && others.includes(a.type),
    );
    primary.sort((a, b) => a.code.localeCompare(b.code));
    rest.sort((a, b) => a.code.localeCompare(b.code));
    return { primary, rest };
  }, [accounts, direction]);

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[560px] max-w-full max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">New transaction</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-900">
            ×
          </button>
        </div>
        <div className="px-5 py-4 space-y-3 overflow-y-auto flex-1">
          <div className="flex gap-2">
            <button
              type="button"
              className={`flex-1 py-2 rounded border text-sm font-medium ${
                direction === "in"
                  ? "bg-emerald-600 text-white border-emerald-700"
                  : "bg-surface text-slate-700 border-slate-300 hover:bg-slate-50"
              }`}
              onClick={() => setDirection("in")}
            >
              Money in
            </button>
            <button
              type="button"
              className={`flex-1 py-2 rounded border text-sm font-medium ${
                direction === "out"
                  ? "bg-rose-600 text-white border-rose-700"
                  : "bg-surface text-slate-700 border-slate-300 hover:bg-slate-50"
              }`}
              onClick={() => setDirection("out")}
            >
              Money out
            </button>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block text-sm">
              <span className="block text-slate-600 mb-1">Amount (AUD)</span>
              <input
                type="number"
                step="0.01"
                min="0"
                className="input"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                onKeyDown={blockScientificNotation}
              />
              {amount.trim() !== "" && !validAmt && (
                <span className="block text-xs text-rose-600 mt-1">
                  Amount must be greater than zero.
                </span>
              )}
            </label>
            <label className="block text-sm">
              <span className="block text-slate-600 mb-1">Date</span>
              <DateInput
                className="input pr-7"
                value={occurredAt}
                onChange={setOccurredAt}
              />
            </label>
          </div>

          <label className="block text-sm">
            <span className="block text-slate-600 mb-1">Counter-party</span>
            <input
              className="input"
              placeholder={
                direction === "in" ? "Who paid you?" : "Who you paid"
              }
              value={counterParty}
              onChange={(e) => setCounterParty(e.target.value)}
            />
          </label>

          <label className="block text-sm">
            <span className="block text-slate-600 mb-1">Memo (optional)</span>
            <input
              className="input"
              value={memo}
              onChange={(e) => setMemo(e.target.value)}
            />
          </label>

          <div className="text-sm">
            <span className="block text-slate-600 mb-1">Category (drives P&amp;L)</span>
            <select
              className="input"
              value={accountId}
              onChange={(e) =>
                setAccountId(e.target.value ? Number(e.target.value) : "")
              }
            >
              <option value="">— Uncategorised —</option>
              {eligible.primary.length > 0 && (
                <optgroup
                  label={direction === "in" ? "Income" : "Expenses / COGS"}
                >
                  {eligible.primary.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.code} · {a.name}
                    </option>
                  ))}
                </optgroup>
              )}
              {/* Balance-sheet accounts are hidden by default so a routine
                  income/expense can't be mis-posted to e.g. Accounts
                  Receivable. Operators who genuinely need them tick the box. */}
              {showOtherAccounts && eligible.rest.length > 0 && (
                <optgroup label="Other (Assets / Liabilities / Equity)">
                  {eligible.rest.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.code} · {a.name}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            {eligible.rest.length > 0 && (
              <label className="mt-1 flex items-center gap-1.5 text-xs text-slate-500">
                <input
                  type="checkbox"
                  checked={showOtherAccounts}
                  onChange={(e) => setShowOtherAccounts(e.target.checked)}
                />
                Show balance-sheet accounts (Assets / Liabilities / Equity)
              </label>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block text-sm">
              <span className="block text-slate-600 mb-1">GST treatment</span>
              <select
                className="input"
                value={taxCode}
                onChange={(e) =>
                  setTaxCode(e.target.value as typeof taxCode)
                }
              >
                <option value="standard">Standard (10% GST)</option>
                <option value="capital">Capital purchase</option>
                <option value="gst_free">GST-free</option>
                <option value="input_taxed">Input-taxed</option>
                <option value="none">Outside BAS (transfer / drawing)</option>
              </select>
            </label>
            <label className="block text-sm">
              <span className="block text-slate-600 mb-1">
                GST portion
                {noGstCodes.has(taxCode) && (
                  <span className="text-xs text-slate-500"> · disabled</span>
                )}
              </span>
              <input
                type="number"
                step="0.01"
                min="0"
                className="input disabled:bg-slate-100"
                value={effectiveGst}
                disabled={noGstCodes.has(taxCode)}
                onChange={(e) => {
                  setGstTouched(true);
                  setGstAmount(e.target.value);
                }}
                onKeyDown={blockScientificNotation}
              />
              {gstBearing && !gstTouched && Number(amount) > 0 && (
                <span className="block text-xs text-slate-500 mt-1">
                  Auto: amount ÷ 11 (GST-inclusive). Edit to override.
                </span>
              )}
              {gstBearing && Number(effectiveGst) > Number(amount || 0) && (
                <span className="block text-xs text-rose-600 mt-1">
                  GST can't exceed the amount.
                </span>
              )}
            </label>
          </div>

          {mut.isError && (
            <p className="text-sm text-rose-600">{apiErrorMessage(mut.error)}</p>
          )}
        </div>
        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={!canSave}
            onClick={submit}
          >
            {mut.isPending ? "Saving…" : "Save transaction"}
          </button>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Import statement dialog (M3)
// ---------------------------------------------------------------------------

type ImportRow = BankImportPreviewRow & {
  // editable overrides applied by the user before commit
  override_account_id: number | null;
  override_tax_code: TaxCode;
  included: boolean;
};

function importRowGstAmount(row: ImportRow): string {
  if (["gst_free", "input_taxed", "none"].includes(row.override_tax_code)) return "0";
  const amount = Number(row.parsed.amount ?? 0);
  if (!Number.isFinite(amount) || amount <= 0) return "0";
  return (amount / 11).toFixed(2);
}

function ImportStatementDialog({
  bankAccountId,
  accounts,
  onClose,
  onImported,
  onCommitted,
}: {
  bankAccountId: number;
  accounts: Account[];
  onClose: () => void;
  onImported: () => void;
  onCommitted: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [bankFormat, setBankFormat] = useState("auto");
  const [preview, setPreview] = useState<BankImportPreview | null>(null);
  const [rows, setRows] = useState<ImportRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const isPdf = !!file && file.name.toLowerCase().endsWith(".pdf");
  const [commitResult, setCommitResult] = useState<BankImportCommitResult | null>(
    null,
  );

  const activeAccounts = useMemo(
    () => accounts.filter((a) => a.active).sort((a, b) => a.code.localeCompare(b.code)),
    [accounts],
  );

  const previewMut = useMutation({
    mutationFn: async (f: File) => {
      const form = new FormData();
      form.append("file", f);
      if (f.name.toLowerCase().endsWith(".pdf")) {
        form.append("bank_format", bankFormat);
      }
      const { data } = await api.post<BankImportPreview>(
        `/bank-accounts/${bankAccountId}/import/preview`,
        form,
        { headers: { "Content-Type": "multipart/form-data" } },
      );
      return data;
    },
    onSuccess: (p) => {
      setPreview(p);
      setRows(
        p.rows.map((r) => ({
          ...r,
          override_account_id: r.suggested_account_id,
          override_tax_code: (r.suggested_tax_code ?? "standard") as TaxCode,
          // Default: include if ok and not duplicate.
          included: r.ok && !r.is_duplicate,
        })),
      );
      setError(null);
    },
    onError: (e) => setError(apiErrorMessage(e)),
  });

  const commitMut = useMutation({
    mutationFn: async () => {
      const payload = {
        rows: rows
          .filter((r) => r.included && r.ok && r.parsed.occurred_at && r.parsed.amount && r.parsed.direction)
          .map((r) => ({
            occurred_at: r.parsed.occurred_at!,
            direction: r.parsed.direction!,
            amount: r.parsed.amount!,
            dedup_key: r.dedup_key,
            account_id: r.override_account_id,
            tax_code: r.override_tax_code,
            memo: r.parsed.memo,
            counter_party_name: r.parsed.counter_party_name,
            gst_amount: importRowGstAmount(r),
          })),
      };
      const { data } = await api.post<BankImportCommitResult>(
        `/bank-accounts/${bankAccountId}/import/commit`,
        payload,
      );
      return data;
    },
    onSuccess: (res) => {
      setCommitResult(res);
      onImported();
    },
    onError: (e) => setError(apiErrorMessage(e)),
  });

  const includedCount = rows.filter((r) => r.included).length;
  const duplicateCount = rows.filter((r) => r.is_duplicate).length;
  const issueCount = rows.filter((r) => !r.ok).length;
  // Hide the Counter-party column when nothing populates it (e.g. CommBank
  // statements keep the payee in the description, so it's always empty).
  const showCounterParty = rows.some(
    (r) => (r.parsed.counter_party_name ?? "").trim() !== "",
  );
  const canPreview = !commitResult && !preview && !!file && !previewMut.isPending;
  const canCommit = !commitResult && !!preview && includedCount > 0 && !commitMut.isPending;

  useModalKeys({
    open: true,
    onClose: commitResult ? onCommitted : onClose,
    onSubmit: () => {
      if (canPreview && file) {
        if (file.size === 0) {
          setError("That file is empty (0 bytes). Choose a file with data.");
          return;
        }
        previewMut.mutate(file);
      } else if (canCommit) commitMut.mutate();
    },
  });

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[min(1100px,95vw)] max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Import bank statement</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-900">
            ×
          </button>
        </div>

        <div className="px-5 py-4 overflow-auto flex-1 space-y-4">
          {commitResult ? (
            <div className="bg-emerald-50 border border-emerald-200 rounded p-4 text-sm">
              <strong>Done.</strong> Created {commitResult.created} transactions
              {commitResult.skipped_duplicates > 0 && (
                <>, skipped {commitResult.skipped_duplicates} duplicates</>
              )}
              .
            </div>
          ) : !preview ? (
            <div className="space-y-3">
              <p className="text-sm text-slate-600">
                Upload a CSV, XLSX or PDF statement exported from your bank. We'll
                detect columns, suggest categories from your rules, and flag any
                rows that look like duplicates of imports you've already done.
              </p>
              <div className="text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded p-3 space-y-1">
                <p className="font-medium text-slate-600">Expected columns (auto-detected, any order):</p>
                <ul className="list-disc list-inside">
                  <li><strong>Date</strong> — e.g. 31/05/2026 (DD/MM/YYYY)</li>
                  <li><strong>Description</strong> / Narrative / Memo</li>
                  <li><strong>Amount</strong> (one signed column) <em>or</em> separate <strong>Debit</strong> &amp; <strong>Credit</strong> columns</li>
                  <li><em>Optional:</em> Balance, Counter-party / Payee</li>
                </ul>
                <p>
                  A header row is recommended for CSV/XLSX. For <strong>PDF</strong>,
                  pick your bank below (or leave on Auto-detect); scanned/image PDFs
                  can't be read — export a CSV/XLSX instead. Always review the parsed
                  rows before importing.
                </p>
              </div>
              <input
                type="file"
                accept=".csv,.xlsx,.pdf"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="block text-sm"
              />
              {isPdf && (
                <label className="block text-sm">
                  <span className="block text-slate-600 mb-1">Bank (for PDF parsing)</span>
                  <select
                    className="input w-64"
                    value={bankFormat}
                    onChange={(e) => setBankFormat(e.target.value)}
                  >
                    <option value="auto">Auto-detect</option>
                    <option value="cba">Commonwealth Bank (CBA)</option>
                    <option value="nab">NAB</option>
                    <option value="anz">ANZ</option>
                    <option value="westpac">Westpac</option>
                  </select>
                </label>
              )}
              {error && <p className="text-sm text-rose-600">{error}</p>}
            </div>
          ) : (
            <>
              <div className="flex items-center gap-4 text-xs text-slate-600">
                <span>
                  <strong>{rows.length}</strong> rows
                </span>
                <span>
                  Will import <strong>{includedCount}</strong>
                </span>
                {duplicateCount > 0 && (
                  <span className="text-amber-700">
                    {duplicateCount} duplicate(s)
                  </span>
                )}
                {issueCount > 0 && (
                  <span className="text-rose-700">
                    {issueCount} row(s) couldn't be parsed
                  </span>
                )}
              </div>

              <div className="overflow-auto border border-slate-200 rounded">
                <table className="w-full min-w-[920px] text-xs">
                  <thead className="bg-slate-50 text-left">
                    <tr>
                      <th className="px-2 py-1.5 w-8"></th>
                      <th className="px-2 py-1.5 w-24">Date</th>
                      <th className="px-2 py-1.5">Memo</th>
                      {showCounterParty && <th className="px-2 py-1.5">Counter-party</th>}
                      <th className="px-2 py-1.5 w-16">Dir</th>
                      <th className="px-2 py-1.5 w-24 text-right">Amount</th>
                      <th className="px-2 py-1.5 w-52">Account</th>
                      <th className="px-2 py-1.5 w-28">Tax</th>
                      <th className="px-2 py-1.5">Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr
                        key={r.row_no}
                        className={`border-t ${
                          !r.ok
                            ? "bg-rose-50"
                            : r.is_duplicate
                              ? "bg-amber-50"
                              : ""
                        }`}
                      >
                        <td className="px-2 py-1">
                          <input
                            type="checkbox"
                            checked={r.included}
                            disabled={!r.ok}
                            onChange={(e) =>
                              setRows((curr) =>
                                curr.map((x, j) =>
                                  j === i ? { ...x, included: e.target.checked } : x,
                                ),
                              )
                            }
                          />
                        </td>
                        <td className="px-2 py-1 font-mono">
                          {r.parsed.occurred_at ? formatDate(r.parsed.occurred_at) : "?"}
                        </td>
                        <td className="px-2 py-1 truncate max-w-[18rem]">
                          {r.parsed.memo ?? ""}
                        </td>
                        {showCounterParty && (
                          <td className="px-2 py-1 truncate max-w-[14rem]">
                            {r.parsed.counter_party_name ?? ""}
                          </td>
                        )}
                        <td className="px-2 py-1">{r.parsed.direction ?? "?"}</td>
                        <td className="px-2 py-1 text-right tabular-nums">
                          {r.parsed.amount ?? "?"}
                        </td>
                        <td className="px-2 py-1">
                          <select
                            className="border rounded px-1 py-0.5 w-full"
                            disabled={!r.ok}
                            value={
                              r.override_account_id === null
                                ? ""
                                : String(r.override_account_id)
                            }
                            onChange={(e) =>
                              setRows((curr) =>
                                curr.map((x, j) =>
                                  j === i
                                    ? {
                                        ...x,
                                        override_account_id:
                                          e.target.value === ""
                                            ? null
                                            : Number(e.target.value),
                                      }
                                    : x,
                                ),
                              )
                            }
                          >
                            <option value="">— uncategorised —</option>
                            {activeAccounts.map((a) => (
                              <option key={a.id} value={a.id}>
                                {a.code} {a.name}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td className="px-2 py-1">
                          <select
                            className="border rounded px-1 py-0.5 w-full"
                            disabled={!r.ok}
                            value={r.override_tax_code}
                            onChange={(e) =>
                              setRows((curr) =>
                                curr.map((x, j) =>
                                  j === i
                                    ? {
                                        ...x,
                                        override_tax_code: e.target.value as TaxCode,
                                      }
                                    : x,
                                ),
                              )
                            }
                          >
                            <option value="standard">standard</option>
                            <option value="capital">capital</option>
                            <option value="gst_free">gst_free</option>
                            <option value="input_taxed">input_taxed</option>
                            <option value="none">none</option>
                          </select>
                        </td>
                        <td className="px-2 py-1 text-slate-500">
                          {r.issue ??
                            (r.is_duplicate ? (
                              "duplicate"
                            ) : r.suggestion_source === "rule" && r.matched_rule_description ? (
                              `rule: ${r.matched_rule_description}`
                            ) : r.suggestion_source === "heuristic" && r.matched_rule_description ? (
                              <span className="text-slate-400">heuristic: {r.matched_rule_description}</span>
                            ) : (
                              ""
                            ))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {error && <p className="text-sm text-rose-600">{error}</p>}
            </>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          {commitResult ? (
            <button className="btn-primary" onClick={onCommitted}>
              Close
            </button>
          ) : !preview ? (
            <>
              <button className="btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                className="btn-primary"
                disabled={!canPreview}
                onClick={() => {
                  if (!file) return;
                  if (file.size === 0) {
                    setError("That file is empty (0 bytes). Choose a file with data.");
                    return;
                  }
                  previewMut.mutate(file);
                }}
              >
                {previewMut.isPending ? "Parsing…" : "Preview"}
              </button>
            </>
          ) : (
            <>
              <button className="btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                className="btn-primary"
                disabled={!canCommit}
                onClick={() => commitMut.mutate()}
              >
                {commitMut.isPending
                  ? "Importing…"
                  : `Import ${includedCount} row(s)`}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
