import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useCompanyStore } from "../store/company";
import { useModalKeys } from "../lib/useModalKeys";
import { apiErrorMessage } from "../lib/errors";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import type {
  Account,
  AccountCreate,
  AccountType,
  AccountUpdate,
} from "../types/api";

const TYPE_LABEL: Record<AccountType, string> = {
  ASSET: "Asset",
  LIABILITY: "Liability",
  EQUITY: "Equity",
  INCOME: "Income",
  EXPENSE: "Expense",
  COST_OF_SALES: "Cost of Sales",
};

const TYPE_ORDER: AccountType[] = [
  "ASSET",
  "LIABILITY",
  "EQUITY",
  "INCOME",
  "COST_OF_SALES",
  "EXPENSE",
];

async function fetchAccounts(): Promise<Account[]> {
  const { data } = await api.get<Account[]>("/accounts");
  return data;
}

async function createAccount(payload: AccountCreate): Promise<Account> {
  const { data } = await api.post<Account>("/accounts", payload);
  return data;
}

async function updateAccount(id: number, payload: AccountUpdate): Promise<Account> {
  const { data } = await api.patch<Account>(`/accounts/${id}`, payload);
  return data;
}

async function deleteAccount(id: number): Promise<void> {
  await api.delete(`/accounts/${id}`);
}

export default function AccountsPage() {
  const currentId = useCompanyStore((s) => s.currentId);
  const qc = useQueryClient();

  const [typeFilter, setTypeFilter] = useState<"all" | AccountType>("all");
  const [showInactive, setShowInactive] = useState(false);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<Account | null>(null);
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Account | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: fetchAccounts,
    enabled: !!currentId,
  });

  const filtered = useMemo(() => {
    if (!data) return [];
    const term = q.trim().toLowerCase();
    return data
      .filter((a) => (showInactive ? true : a.active))
      .filter((a) => (typeFilter === "all" ? true : a.type === typeFilter))
      .filter(
        (a) =>
          !term ||
          a.code.toLowerCase().includes(term) ||
          a.name.toLowerCase().includes(term) ||
          (a.description ?? "").toLowerCase().includes(term),
      );
  }, [data, q, typeFilter, showInactive]);

  const grouped = useMemo(() => {
    const m = new Map<AccountType, Account[]>();
    for (const t of TYPE_ORDER) m.set(t, []);
    for (const a of filtered) {
      const t = a.type as AccountType;
      if (!m.has(t)) m.set(t, []);
      m.get(t)!.push(a);
    }
    for (const arr of m.values()) arr.sort((x, y) => x.code.localeCompare(y.code));
    return m;
  }, [filtered]);

  const toggleActive = useMutation({
    mutationFn: (a: Account) => updateAccount(a.id, { active: !a.active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const remove = useMutation({
    mutationFn: (a: Account) => deleteAccount(a.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">Pick a company in the top bar.</p>
      </div>
    );
  }

  const totalActive = data?.filter((a) => a.active).length ?? 0;
  const totalAll = data?.length ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">Chart of Accounts</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Australian SME structure. Used by bank-transaction categorisation, P&amp;L and BAS.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search code, name…"
            className="border rounded px-2 py-1 text-sm w-56"
          />
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value as "all" | AccountType)}
            className="border rounded px-2 py-1 text-sm"
          >
            <option value="all">All types</option>
            {TYPE_ORDER.map((t) => (
              <option key={t} value={t}>
                {TYPE_LABEL[t]}
              </option>
            ))}
          </select>
          <label className="text-xs flex items-center gap-1">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={(e) => setShowInactive(e.target.checked)}
            />
            Show inactive
          </label>
          <button
            onClick={() => setCreating(true)}
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800"
          >
            + New Account
          </button>
        </div>
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        <div className="px-4 py-3 border-b bg-slate-50 flex justify-between text-sm">
          <span>
            <strong>{filtered.length}</strong> shown · {totalActive} active /{" "}
            {totalAll} total
          </span>
          {deleteError && (
            <span className="text-rose-700 text-xs">{deleteError}</span>
          )}
        </div>

        {isLoading ? (
          <div className="px-4 py-6 text-sm text-slate-500">Loading…</div>
        ) : error ? (
          <div className="px-4 py-6 text-sm text-rose-700">
            {(error as Error).message}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-500 border-b bg-slate-50">
              <tr>
                <th className="py-2 px-3 w-24">Code</th>
                <th className="py-2 px-3">Name</th>
                <th className="py-2 px-3 w-24">Type</th>
                <th className="py-2 px-3 w-20">GST</th>
                <th className="py-2 px-3 w-20">Active</th>
                <th className="py-2 px-3 w-44 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {TYPE_ORDER.flatMap((t) =>
                (grouped.get(t) ?? []).map((a) => (
                  <tr
                    key={a.id}
                    className={`border-b last:border-b-0 ${
                      a.active ? "" : "bg-slate-50 text-slate-400"
                    }`}
                  >
                    <td className="py-1.5 px-3 font-mono">{a.code}</td>
                    <td className="py-1.5 px-3">
                      <div>{a.name}</div>
                      {a.description && (
                        <div className="text-xs text-slate-500">{a.description}</div>
                      )}
                    </td>
                    <td className="py-1.5 px-3 text-xs text-slate-500">
                      {TYPE_LABEL[a.type as AccountType]}
                    </td>
                    <td className="py-1.5 px-3">{a.is_gst ? "✓" : ""}</td>
                    <td className="py-1.5 px-3">{a.active ? "✓" : "—"}</td>
                    <td className="py-1.5 px-3 text-right space-x-2">
                      <button
                        onClick={() => setEditing(a)}
                        className="text-xs text-slate-600 hover:text-slate-900 underline"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => toggleActive.mutate(a)}
                        disabled={toggleActive.isPending}
                        className="text-xs text-slate-600 hover:text-slate-900 underline"
                      >
                        {a.active ? "Deactivate" : "Activate"}
                      </button>
                      <button
                        onClick={() => {
                          setDeleteError(null);
                          setPendingDelete(a);
                        }}
                        disabled={remove.isPending}
                        className="text-xs text-rose-600 hover:text-rose-800 underline"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                )),
              )}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                    No accounts match these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <AccountFormDialog
          existing={editing}
          allAccounts={data ?? []}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["accounts"] });
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        destructive
        title="Delete account?"
        message={
          pendingDelete
            ? `Delete account ${pendingDelete.code} ${pendingDelete.name}? This only works if it has never been used.`
            : ""
        }
        confirmLabel="Delete"
        busy={remove.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          remove.mutate(pendingDelete, {
            onSuccess: () => setPendingDelete(null),
            onError: (e) => {
              setDeleteError(apiErrorMessage(e));
              setPendingDelete(null);
            },
          });
        }}
      />
    </div>
  );
}

function AccountFormDialog({
  existing,
  allAccounts,
  onClose,
  onSaved,
}: {
  existing: Account | null;
  allAccounts: Account[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = !!existing;
  const [code, setCode] = useState(existing?.code ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  // Mirror the backend pattern ^[A-Za-z0-9._-]+$ for inline feedback.
  const codeError =
    code.trim() && !/^[A-Za-z0-9._-]+$/.test(code.trim())
      ? "Use only letters, numbers, dot, dash or underscore (no spaces or symbols)."
      : null;
  const [type, setType] = useState<AccountType>(
    (existing?.type as AccountType) ?? "EXPENSE",
  );
  const [parentId, setParentId] = useState<number | "">(existing?.parent_id ?? "");
  const [isGst, setIsGst] = useState(existing?.is_gst ?? false);
  const [active, setActive] = useState(existing?.active ?? true);
  const [description, setDescription] = useState(existing?.description ?? "");
  const [submitError, setSubmitError] = useState<string | null>(null);

  const parentChoices = useMemo(
    () =>
      allAccounts
        .filter((a) => a.id !== existing?.id && a.type === type)
        .sort((a, b) => a.code.localeCompare(b.code)),
    [allAccounts, type, existing?.id],
  );

  const submit = () => {
    setSubmitError(null);
    if (!code.trim() || !name.trim()) {
      setSubmitError("Code and name are required.");
      return;
    }
    // Surface a duplicate code immediately, before the round-trip. The backend
    // also enforces uniqueness (409) as the source of truth.
    const codeTaken = allAccounts.some(
      (a) => a.id !== existing?.id && a.code.trim() === code.trim(),
    );
    if (codeTaken) {
      setSubmitError(`Account code "${code.trim()}" already exists.`);
      return;
    }
    save.mutate();
  };

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        const payload: AccountUpdate = {
          code: code.trim(),
          name: name.trim(),
          type,
          is_gst: isGst,
          active,
          description: description.trim() || null,
        };
        if (parentId === "" || parentId === null) {
          payload.set_parent_null = true;
        } else {
          payload.parent_id = parentId as number;
        }
        return updateAccount(existing!.id, payload);
      } else {
        const payload: AccountCreate = {
          code: code.trim(),
          name: name.trim(),
          type,
          is_gst: isGst,
          description: description.trim() || null,
          parent_id: parentId === "" ? null : (parentId as number),
        };
        return createAccount(payload);
      }
    },
    onSuccess: () => onSaved(),
    onError: (e) => {
      setSubmitError(apiErrorMessage(e));
    },
  });

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 z-40 bg-slate-900/30 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-full max-w-lg max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b flex justify-between items-center">
          <h3 className="font-semibold">
            {isEdit ? `Edit account ${existing!.code}` : "New account"}
          </h3>
          <button
            onClick={onClose}
            className="text-slate-500 hover:text-slate-900"
          >
            ✕
          </button>
        </div>
        {submitError && (
          <div className="mx-5 mt-3 text-rose-700 text-xs bg-rose-50 border border-rose-200 rounded px-3 py-2">
            {submitError}
          </div>
        )}
        <div className="p-5 space-y-3 text-sm overflow-y-auto flex-1">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Code *">
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className={`border rounded px-2 py-1 w-full ${codeError ? "border-rose-400" : ""}`}
                placeholder="e.g. 6420"
                maxLength={20}
              />
              {codeError && (
                <span className="mt-0.5 block text-[11px] text-rose-600">{codeError}</span>
              )}
            </Field>
            <Field label="Type">
              <select
                value={type}
                onChange={(e) => {
                  setType(e.target.value as AccountType);
                  setParentId("");
                }}
                className="border rounded px-2 py-1 w-full"
              >
                {TYPE_ORDER.map((t) => (
                  <option key={t} value={t}>
                    {TYPE_LABEL[t]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Field label="Name *">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="border rounded px-2 py-1 w-full"
              maxLength={120}
            />
          </Field>
          <Field label="Description (optional)">
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="border rounded px-2 py-1 w-full"
            />
          </Field>
          <Field label="Parent account (same type only)">
            <select
              value={parentId === "" ? "" : String(parentId)}
              onChange={(e) =>
                setParentId(e.target.value === "" ? "" : Number(e.target.value))
              }
              className="border rounded px-2 py-1 w-full"
            >
              <option value="">— None —</option>
              {parentChoices.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.code} — {a.name}
                </option>
              ))}
            </select>
          </Field>
          <div className="flex items-center gap-4 pt-1">
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={isGst}
                onChange={(e) => setIsGst(e.target.checked)}
              />
              <span>Is a GST account</span>
            </label>
            {isEdit && (
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={active}
                  onChange={(e) => setActive(e.target.checked)}
                />
                <span>Active</span>
              </label>
            )}
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
            onClick={submit}
            disabled={save.isPending || !code.trim() || !name.trim() || !!codeError}
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {save.isPending ? "Saving…" : isEdit ? "Save changes" : "Create account"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs text-slate-500 block mb-0.5">{label}</span>
      {children}
    </label>
  );
}
