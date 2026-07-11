import { useState, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { apiErrorMessage } from "../lib/errors";
import { toast } from "../lib/toast";
import { useModalKeys } from "../lib/useModalKeys";
import { useCompanyStore } from "../store/company";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import type {
  Account,
  BankRule,
  BankRuleCreate,
  BankRuleUpdate,
  TaxCode,
} from "../types/api";

async function fetchRules(): Promise<BankRule[]> {
  const { data } = await api.get<BankRule[]>("/bank-rules");
  return data;
}

async function fetchAccounts(): Promise<Account[]> {
  const { data } = await api.get<Account[]>("/accounts");
  return data;
}

export default function BankRulesPage() {
  const currentId = useCompanyStore((s) => s.currentId);
  const qc = useQueryClient();
  const [editing, setEditing] = useState<BankRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<BankRule | null>(null);

  const rulesQ = useQuery({
    queryKey: ["bank-rules", currentId],
    queryFn: fetchRules,
    enabled: !!currentId,
  });
  const accountsQ = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: fetchAccounts,
    enabled: !!currentId,
  });

  const accountsById = useMemo(() => {
    const m = new Map<number, Account>();
    (accountsQ.data ?? []).forEach((a) => m.set(a.id, a));
    return m;
  }, [accountsQ.data]);

  const remove = useMutation({
    mutationFn: (rule: BankRule) =>
      api.delete(`/bank-rules/${rule.id}`).then(() => undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bank-rules"] }),
    onError: (e) => toast(`Could not delete rule: ${apiErrorMessage(e)}`, "error"),
  });

  const toggleActive = useMutation({
    mutationFn: (rule: BankRule) =>
      api
        .patch<BankRule>(`/bank-rules/${rule.id}`, {
          is_active: !rule.is_active,
        })
        .then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bank-rules"] }),
    onError: (e) => toast(`Could not update rule: ${apiErrorMessage(e)}`, "error"),
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
          <h1 className="text-xl font-semibold">Bank rules</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Auto-categorise incoming bank transactions during statement import.
            Lower priority numbers match first.
          </p>
        </div>
        <button
          className="btn-primary"
          onClick={() => setCreating(true)}
          disabled={!accountsQ.data}
        >
          + New rule
        </button>
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        {rulesQ.isLoading || accountsQ.isLoading ? (
          <div className="px-4 py-6 text-sm text-slate-500">Loading…</div>
        ) : (rulesQ.data ?? []).length === 0 ? (
          <div className="px-4 py-10 text-center text-sm text-slate-500">
            No rules yet. Imported transactions will start uncategorised until
            you add some.
          </div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[720px]">
            <thead className="text-left text-slate-500 border-b bg-slate-50">
              <tr>
                <th className="py-2 px-3 w-12">Pri</th>
                <th className="py-2 px-3">Description</th>
                <th className="py-2 px-3 w-16">Dir</th>
                <th className="py-2 px-3 w-32">Memo match</th>
                <th className="py-2 px-3 w-44">→ Account</th>
                <th className="py-2 px-3 w-28">Tax</th>
                <th className="py-2 px-3 w-16">Active</th>
                <th className="py-2 px-3 w-36 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(rulesQ.data ?? []).map((r) => {
                const acc = accountsById.get(r.set_account_id);
                return (
                  <tr
                    key={r.id}
                    className={`border-b last:border-b-0 ${
                      r.is_active ? "" : "bg-slate-50 text-slate-400"
                    }`}
                  >
                    <td className="py-1.5 px-3 font-mono">{r.priority}</td>
                    <td className="py-1.5 px-3">{r.description}</td>
                    <td className="py-1.5 px-3 text-xs">
                      {r.match_direction ?? "any"}
                    </td>
                    <td className="py-1.5 px-3 font-mono text-xs truncate max-w-[12rem]">
                      {r.match_memo_regex ?? ""}
                    </td>
                    <td className="py-1.5 px-3">
                      {acc ? (
                        <span>
                          <span className="font-mono text-xs text-slate-500 mr-1">
                            {acc.code}
                          </span>
                          {acc.name}
                        </span>
                      ) : (
                        `#${r.set_account_id}`
                      )}
                    </td>
                    <td className="py-1.5 px-3 text-xs whitespace-nowrap">{r.set_tax_code}</td>
                    <td className="py-1.5 px-3">{r.is_active ? "✓" : "—"}</td>
                    <td className="py-1.5 px-3 text-right space-x-2">
                      <button
                        onClick={() => setEditing(r)}
                        className="text-xs text-slate-600 hover:text-slate-900 underline"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => toggleActive.mutate(r)}
                        className="text-xs text-slate-600 hover:text-slate-900 underline"
                      >
                        {r.is_active ? "Disable" : "Enable"}
                      </button>
                      <button
                        onClick={() => setPendingDelete(r)}
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
        <RuleDialog
          existing={editing}
          accounts={accountsQ.data}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["bank-rules"] });
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        destructive
        title="Delete rule?"
        message={pendingDelete ? `Delete rule "${pendingDelete.description}"?` : ""}
        confirmLabel="Delete"
        busy={remove.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          remove.mutate(pendingDelete, {
            onSettled: () => setPendingDelete(null),
          });
        }}
      />
    </div>
  );
}


function RuleDialog({
  existing,
  accounts,
  onClose,
  onSaved,
}: {
  existing: BankRule | null;
  accounts: Account[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = !!existing;
  const [priority, setPriority] = useState<number>(existing?.priority ?? 100);
  const [description, setDescription] = useState(existing?.description ?? "");
  const [direction, setDirection] = useState<"" | "in" | "out">(
    (existing?.match_direction as "in" | "out" | null) ?? "",
  );
  const [memoRegex, setMemoRegex] = useState(existing?.match_memo_regex ?? "");
  const [counterRegex, setCounterRegex] = useState(
    existing?.match_counter_party_regex ?? "",
  );
  const [minAmt, setMinAmt] = useState(existing?.match_amount_min ?? "");
  const [maxAmt, setMaxAmt] = useState(existing?.match_amount_max ?? "");
  const [setAccountId, setSetAccountId] = useState<number | "">(
    existing?.set_account_id ?? "",
  );
  const [setTaxCode, setSetTaxCode] = useState<TaxCode>(
    (existing?.set_tax_code as TaxCode) ?? "standard",
  );
  const [error, setError] = useState<string | null>(null);

  const activeAccounts = accounts
    .filter((a) => a.active)
    .sort((a, b) => a.code.localeCompare(b.code));

  const submit = () => {
    setError(null);
    if (!description.trim()) {
      setError("Description is required");
      return;
    }
    if (setAccountId === "") {
      setError("Pick a target account");
      return;
    }
    save.mutate();
  };

  const save = useMutation({
    mutationFn: async () => {
      const payload: BankRuleCreate = {
        priority,
        description: description.trim(),
        match_direction: direction || null,
        match_amount_min: minAmt || null,
        match_amount_max: maxAmt || null,
        match_memo_regex: memoRegex.trim() || null,
        match_counter_party_regex: counterRegex.trim() || null,
        set_account_id: setAccountId as number,
        set_tax_code: setTaxCode,
      };
      if (isEdit) {
        const { data } = await api.patch<BankRule>(
          `/bank-rules/${existing!.id}`,
          payload as BankRuleUpdate,
        );
        return data;
      }
      const { data } = await api.post<BankRule>("/bank-rules", payload);
      return data;
    },
    onSuccess: () => onSaved(),
    onError: (e) => setError(apiErrorMessage(e)),
  });

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[640px] max-w-full max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b flex items-center justify-between">
          <h2 className="text-lg font-semibold">
            {isEdit ? "Edit rule" : "New rule"}
          </h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-900">
            ×
          </button>
        </div>
        {error && (
          <div className="mx-5 mt-3 text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded px-3 py-2">
            {error}
          </div>
        )}
        <div className="px-5 py-4 space-y-3 text-sm overflow-y-auto flex-1">
          <label className="block">
            <span className="block text-slate-600 mb-1">Description</span>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="input"
              placeholder="e.g. Office rent → 6100"
            />
          </label>
          <div className="grid grid-cols-3 gap-3">
            <label className="block">
              <span className="block text-slate-600 mb-1">Priority</span>
              <input
                type="number"
                min={0}
                value={priority}
                onChange={(e) => setPriority(Number(e.target.value))}
                className="input"
              />
            </label>
            <label className="block">
              <span className="block text-slate-600 mb-1">Direction</span>
              <select
                value={direction}
                onChange={(e) => setDirection(e.target.value as typeof direction)}
                className="input"
              >
                <option value="">Any</option>
                <option value="in">In only</option>
                <option value="out">Out only</option>
              </select>
            </label>
            <label className="block">
              <span className="block text-slate-600 mb-1">Tax code</span>
              <select
                value={setTaxCode}
                onChange={(e) => setSetTaxCode(e.target.value as TaxCode)}
                className="input"
              >
                <option value="standard">standard</option>
                <option value="capital">capital</option>
                <option value="gst_free">gst_free</option>
                <option value="input_taxed">input_taxed</option>
                <option value="none">none</option>
              </select>
            </label>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="block text-slate-600 mb-1">
                Memo regex (optional, case-insensitive)
              </span>
              <input
                value={memoRegex}
                onChange={(e) => setMemoRegex(e.target.value)}
                className="input font-mono text-xs"
                placeholder="e.g. (?i)rent|lease"
              />
            </label>
            <label className="block">
              <span className="block text-slate-600 mb-1">
                Counter-party regex (optional)
              </span>
              <input
                value={counterRegex}
                onChange={(e) => setCounterRegex(e.target.value)}
                className="input font-mono text-xs"
              />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="block text-slate-600 mb-1">Min amount (optional)</span>
              <input
                value={minAmt ?? ""}
                onChange={(e) => setMinAmt(e.target.value)}
                className="input"
                placeholder="0.00"
              />
            </label>
            <label className="block">
              <span className="block text-slate-600 mb-1">Max amount (optional)</span>
              <input
                value={maxAmt ?? ""}
                onChange={(e) => setMaxAmt(e.target.value)}
                className="input"
                placeholder="9999.99"
              />
            </label>
          </div>
          <label className="block">
            <span className="block text-slate-600 mb-1">→ Set account</span>
            <select
              value={setAccountId === "" ? "" : String(setAccountId)}
              onChange={(e) =>
                setSetAccountId(e.target.value === "" ? "" : Number(e.target.value))
              }
              className="input"
            >
              <option value="">— pick account —</option>
              {activeAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.code} — {a.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="px-5 py-3 border-t flex justify-end gap-2 bg-slate-50">
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={submit}
            disabled={save.isPending}
          >
            {save.isPending ? "Saving…" : isEdit ? "Save changes" : "Create rule"}
          </button>
        </div>
      </div>
    </div>
  );
}
