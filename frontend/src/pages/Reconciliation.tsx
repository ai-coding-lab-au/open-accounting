import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useCompanyStore } from "../store/company";
import { formatDate, formatMoney } from "../lib/format";
import { apiErrorMessage } from "../lib/errors";
import type {
  Account,
  BankTransaction,
  TaxCode,
} from "../types/api";


async function fetchUncategorised(): Promise<BankTransaction[]> {
  const { data } = await api.get<BankTransaction[]>(
    "/bank-accounts/transactions/uncategorised",
  );
  return data;
}

async function fetchAccounts(): Promise<Account[]> {
  const { data } = await api.get<Account[]>("/accounts");
  return data;
}


export default function ReconciliationPage() {
  const currentId = useCompanyStore((s) => s.currentId);

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

  return (
    <div className="space-y-6">
      <UncategorisedSection />
    </div>
  );
}


// ---------------------------------------------------------------------------
// Section: uncategorised bank transactions (M3)
// ---------------------------------------------------------------------------

function gstForTax(amount: string, taxCode: TaxCode): string {
  if (["gst_free", "input_taxed", "none"].includes(taxCode)) return "0";
  return (Number(amount || 0) / 11).toFixed(2);
}

function UncategorisedSection() {
  const currentId = useCompanyStore((s) => s.currentId);
  const qc = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [batchAccountId, setBatchAccountId] = useState<number | "">("");
  const [batchTaxCode, setBatchTaxCode] = useState<TaxCode>("standard");
  const [batchError, setBatchError] = useState<string | null>(null);
  const [batchShowOther, setBatchShowOther] = useState(false);

  const txnsQ = useQuery({
    queryKey: ["uncategorised-txns", currentId],
    queryFn: fetchUncategorised,
    enabled: !!currentId,
  });
  const accountsQ = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: fetchAccounts,
    enabled: !!currentId,
  });

  const activeAccounts = useMemo(
    () => (accountsQ.data ?? []).filter((a) => a.active).sort((a, b) => a.code.localeCompare(b.code)),
    [accountsQ.data],
  );

  // A bulk selection can mix in/out transactions, so we can't filter by a
  // single direction — but we still hide balance-sheet accounts by default so
  // a routine categorisation can't land on Assets/Liabilities/Equity. The
  // operator opts in with a checkbox.
  const batchPnl = useMemo(
    () =>
      activeAccounts.filter((a) =>
        ["INCOME", "EXPENSE", "COST_OF_SALES"].includes(a.type),
      ),
    [activeAccounts],
  );
  const batchOther = useMemo(
    () =>
      activeAccounts.filter((a) =>
        ["ASSET", "LIABILITY", "EQUITY"].includes(a.type),
      ),
    [activeAccounts],
  );

  const txns = txnsQ.data ?? [];
  const selectedCount = selectedIds.size;
  const allSelected = txns.length > 0 && txns.every((t) => selectedIds.has(t.id));

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["uncategorised-txns"] });
    qc.invalidateQueries({ queryKey: ["bank-transactions"] });
    qc.invalidateQueries({ queryKey: ["bank-accounts"] });
    qc.invalidateQueries({ queryKey: ["dashboard"] });
    qc.invalidateQueries({ queryKey: ["trial-balance"] });
    qc.invalidateQueries({ queryKey: ["balance-sheet"] });
  };

  const batchMut = useMutation({
    mutationFn: async () => {
      if (batchAccountId === "") return;
      const selectedTxns = txns.filter((t) => selectedIds.has(t.id));
      await Promise.all(
        selectedTxns.map((txn) =>
          api.patch<BankTransaction>(`/bank-accounts/transactions/${txn.id}/categorise`, {
            account_id: batchAccountId,
            tax_code: batchTaxCode,
            gst_amount: gstForTax(txn.amount, batchTaxCode),
          }),
        ),
      );
    },
    onSuccess: () => {
      setSelectedIds(new Set());
      setBatchError(null);
      invalidate();
    },
    onError: (e) => setBatchError(apiErrorMessage(e)),
  });

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">Reconciliation</h2>
        <p className="text-xs text-slate-500 mt-0.5">
          Uncategorised bank transactions — imported transactions that don't
          yet have an account assigned. While any of these exist, the Trial
          Balance and Balance Sheet will be out of balance. Assign each one to
          make your books balance.
        </p>
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        {txns.length > 0 && (
        <div
          className={`border-b px-4 py-3 flex flex-wrap items-end gap-3 text-sm ${
            selectedCount > 0
              ? "bg-emerald-50 border-emerald-200"
              : "bg-slate-50 border-slate-200 opacity-70"
          }`}
        >
          <div
            className={`font-medium pb-1 min-w-64 ${
              selectedCount > 0 ? "text-emerald-900" : "text-slate-500"
            }`}
          >
            {selectedCount > 0
              ? `${selectedCount} selected`
              : "Select rows to apply an account or tax code in bulk"}
          </div>
            <label className="block min-w-64">
              <span className="block text-xs text-slate-600 mb-1">Apply account</span>
              <select
                value={batchAccountId === "" ? "" : String(batchAccountId)}
                onChange={(e) => setBatchAccountId(e.target.value === "" ? "" : Number(e.target.value))}
                disabled={selectedCount === 0}
                className="border rounded px-2 py-1 w-full text-xs bg-surface"
              >
                <option value="">— pick —</option>
                <optgroup label="Income / Expenses / COGS">
                  {batchPnl.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.code} {a.name}
                    </option>
                  ))}
                </optgroup>
                {batchShowOther && batchOther.length > 0 && (
                  <optgroup label="Other (Assets / Liabilities / Equity)">
                    {batchOther.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.code} {a.name}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
              {batchOther.length > 0 && (
                <label className="mt-0.5 flex items-center gap-1 text-[10px] text-slate-400">
                  <input
                    type="checkbox"
                    checked={batchShowOther}
                    onChange={(e) => setBatchShowOther(e.target.checked)}
                    disabled={selectedCount === 0}
                  />
                  balance-sheet accounts
                </label>
              )}
            </label>
            <label className="block min-w-36">
              <span className="block text-xs text-slate-600 mb-1">Apply tax</span>
              <select
                value={batchTaxCode}
                onChange={(e) => setBatchTaxCode(e.target.value as TaxCode)}
                disabled={selectedCount === 0}
                className="border rounded px-2 py-1 w-full text-xs bg-surface"
              >
                <option value="standard">standard</option>
                <option value="capital">capital</option>
                <option value="gst_free">gst_free</option>
                <option value="input_taxed">input_taxed</option>
                <option value="none">none</option>
              </select>
            </label>
            <button
              className="btn-primary text-xs"
              disabled={selectedCount === 0 || batchAccountId === "" || batchMut.isPending}
              onClick={() => {
                setBatchError(null);
                batchMut.mutate();
              }}
            >
              {batchMut.isPending ? "Applying..." : "Apply"}
            </button>
            <button
              className="btn-secondary text-xs"
              disabled={selectedCount === 0 || batchMut.isPending}
              onClick={() => setSelectedIds(new Set())}
            >
              Clear
            </button>
            {batchError && <span className="text-xs text-rose-700 pb-1">{batchError}</span>}
          </div>
        )}
        {txnsQ.isLoading || accountsQ.isLoading ? (
          <div className="px-4 py-6 text-sm text-slate-500">Loading…</div>
        ) : txns.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-emerald-700">
            ✓ All bank transactions are categorised.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-500 border-b bg-slate-50">
              <tr>
                <th className="py-2 px-3 w-10">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={(e) =>
                      setSelectedIds(e.target.checked ? new Set(txns.map((t) => t.id)) : new Set())
                    }
                  />
                </th>
                <th className="py-2 px-3 w-24">Date</th>
                <th className="py-2 px-3 w-12">Dir</th>
                <th className="py-2 px-3 w-28 text-right">Amount</th>
                <th className="py-2 px-3">Memo</th>
                <th className="py-2 px-3 w-56">Account</th>
                <th className="py-2 px-3 w-32">Tax</th>
                <th className="py-2 px-3 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {txns.map((t) => (
                <UncategorisedRow
                  key={t.id}
                  txn={t}
                  accounts={activeAccounts}
                  selected={selectedIds.has(t.id)}
                  onToggle={(checked) =>
                    setSelectedIds((curr) => {
                      const next = new Set(curr);
                      if (checked) next.add(t.id);
                      else next.delete(t.id);
                      return next;
                    })
                  }
                  onSaved={invalidate}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}


function UncategorisedRow({
  txn,
  accounts,
  selected,
  onToggle,
  onSaved,
}: {
  txn: BankTransaction;
  accounts: Account[];
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onSaved: () => void;
}) {
  const [accountId, setAccountId] = useState<number | "">(txn.account_id ?? "");
  const [taxCode, setTaxCode] = useState<TaxCode>(txn.tax_code);
  const [err, setErr] = useState<string | null>(null);
  const [showOther, setShowOther] = useState(false);

  // Filter by direction so a deposit can't be mis-posted to an expense (and
  // vice-versa); balance-sheet accounts are hidden behind an opt-in toggle.
  // Mirrors the "+ New transaction" modal's behaviour.
  const { primary, rest } = useMemo(() => {
    const primaryTypes =
      txn.direction === "in"
        ? new Set(["INCOME"])
        : new Set(["EXPENSE", "COST_OF_SALES"]);
    const others = new Set(["ASSET", "LIABILITY", "EQUITY"]);
    return {
      primary: accounts.filter((a) => primaryTypes.has(a.type)),
      rest: accounts.filter((a) => others.has(a.type)),
    };
  }, [accounts, txn.direction]);

  const save = useMutation({
    mutationFn: async (vars: { account_id: number; tax_code: TaxCode }) => {
      const { data } = await api.patch<BankTransaction>(
        `/bank-accounts/transactions/${txn.id}/categorise`,
        {
          account_id: vars.account_id,
          tax_code: vars.tax_code,
          gst_amount: gstForTax(txn.amount, vars.tax_code),
        },
      );
      return data;
    },
    onSuccess: () => onSaved(),
    onError: (e) => setErr(apiErrorMessage(e)),
  });

  // Selecting an account (or changing the tax code once an account is set) saves
  // the row immediately — the inline dropdown IS the save, no extra click. Once
  // saved the row is categorised and drops out of this list.
  const saveWith = (account: number | "", tax: TaxCode) => {
    setErr(null);
    if (account !== "") save.mutate({ account_id: account, tax_code: tax });
  };

  return (
    <tr className="border-b last:border-b-0">
      <td className="py-1.5 px-3">
        <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
      </td>
      <td className="py-1.5 px-3 font-mono text-xs">{formatDate(txn.occurred_at)}</td>
      <td className="py-1.5 px-3 text-xs">{txn.direction}</td>
      <td className="py-1.5 px-3 text-right tabular-nums">
        {formatMoney(txn.amount)}
      </td>
      <td className="py-1.5 px-3 truncate max-w-[20rem]">
        {txn.memo ?? ""}
        {txn.counter_party_name && (
          <span className="text-xs text-slate-500"> · {txn.counter_party_name}</span>
        )}
        {err && <span className="text-xs text-rose-600 block">{err}</span>}
      </td>
      <td className="py-1.5 px-3">
        <select
          value={accountId === "" ? "" : String(accountId)}
          onChange={(e) => {
            const v = e.target.value === "" ? "" : Number(e.target.value);
            setAccountId(v);
            saveWith(v, taxCode);
          }}
          disabled={save.isPending}
          className="border rounded px-1 py-0.5 w-full text-xs"
        >
          <option value="">— pick —</option>
          {primary.length > 0 && (
            <optgroup label={txn.direction === "in" ? "Income" : "Expenses / COGS"}>
              {primary.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.code} {a.name}
                </option>
              ))}
            </optgroup>
          )}
          {showOther && rest.length > 0 && (
            <optgroup label="Other (Assets / Liabilities / Equity)">
              {rest.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.code} {a.name}
                </option>
              ))}
            </optgroup>
          )}
        </select>
        {rest.length > 0 && (
          <label className="mt-0.5 flex items-center gap-1 text-[10px] text-slate-400">
            <input
              type="checkbox"
              checked={showOther}
              onChange={(e) => setShowOther(e.target.checked)}
            />
            balance-sheet
          </label>
        )}
      </td>
      <td className="py-1.5 px-3">
        <select
          value={taxCode}
          onChange={(e) => {
            const tc = e.target.value as TaxCode;
            setTaxCode(tc);
            saveWith(accountId, tc);
          }}
          disabled={save.isPending}
          className="border rounded px-1 py-0.5 w-full min-w-[110px] text-xs"
        >
          <option value="standard">standard</option>
          <option value="capital">capital</option>
          <option value="gst_free">gst_free</option>
          <option value="input_taxed">input_taxed</option>
          <option value="none">none</option>
        </select>
      </td>
      <td className="py-1.5 px-3 text-right">
        {save.isPending ? (
          <span className="text-xs text-slate-400">Saving…</span>
        ) : accountId === "" ? (
          <span className="text-xs text-slate-400">Pick account</span>
        ) : (
          <span className="text-xs text-emerald-600">✓</span>
        )}
      </td>
    </tr>
  );
}

