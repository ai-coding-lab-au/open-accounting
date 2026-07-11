import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import { useCompanyStore } from "../../store/company";
import { usePrivacyEnabled } from "../../lib/usePrivacy";
import { maskName } from "../../lib/mask";
import type { Company, CompanyCreate } from "../../types/api";

async function fetchCompanies(): Promise<Company[]> {
  const { data } = await api.get<Company[]>("/companies");
  return data;
}

async function createCompany(payload: CompanyCreate): Promise<Company> {
  const { data } = await api.post<Company>("/companies", payload);
  return data;
}

async function deleteCompany(id: string): Promise<void> {
  // confirm must match the id — mirrors the backend's ?confirm guard.
  await api.delete(`/companies/${encodeURIComponent(id)}`, {
    params: { confirm: id },
  });
}

export default function CompanySwitcher() {
  const qc = useQueryClient();
  const { currentId, currentGeneration, setCurrent } = useCompanyStore();
  const [showCreate, setShowCreate] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const privacyOn = usePrivacyEnabled();

  const { data: companies, isLoading, isFetching } = useQuery({
    queryKey: ["companies"],
    queryFn: fetchCompanies,
  });

  // Track whether we've already prompted the user to create the first
  // company, so a refetch returning [] briefly (or the in-flight POST
  // refetching while companies is still []) doesn't re-pop the dialog
  // after the user just submitted one.
  const [autoPromptShown, setAutoPromptShown] = useState(false);

  useEffect(() => {
    // Only auto-open once, and only when the backend is reachable (companies is a real array).
    if (!isLoading && Array.isArray(companies) && companies.length === 0 && !autoPromptShown && !showCreate) {
      setShowCreate(true);
      setAutoPromptShown(true);
    }
    // Auto-pick the first company when none is selected, OR when the persisted
    // currentId points at a company that no longer exists (e.g. deleted in
    // another session). Gate on !isFetching: a refetch / hot-reload can briefly
    // return a partial or empty list, and self-healing on that transient state
    // would wrongly reset the user's selected company to companies[0].
    if (Array.isArray(companies) && !isFetching) {
      if (companies.length === 0) {
        if (currentId !== null || currentGeneration !== null) setCurrent(null);
      } else {
        const selected = companies.find((company) => company.id === currentId);
        const next = selected ?? companies[0];
        if (
          currentId !== next.id ||
          currentGeneration !== next.generation_id
        ) {
          setCurrent(next);
        }
      }
    }
  }, [
    companies,
    currentId,
    currentGeneration,
    isLoading,
    isFetching,
    setCurrent,
    autoPromptShown,
    showCreate,
  ]);

  const createMut = useMutation({
    mutationFn: createCompany,
    onSuccess: async (company) => {
      // Refetch the company list and WAIT for it to finish before selecting
      // the new company. Otherwise the auto-pick effect runs while the list
      // still lacks the new id, decides currentId is "invalid", and resets it
      // to companies[0] — so a just-created company never becomes active.
      await qc.refetchQueries({ queryKey: ["companies"] });
      setCurrent(company);
      setShowCreate(false);
    },
  });

  const deleteMut = useMutation({
    mutationFn: deleteCompany,
    onSuccess: async (_void, deletedId) => {
      // If we just deleted the active company, clear the selection so the
      // auto-pick effect can choose another (or prompt create if none remain).
      if (currentId === deletedId) setCurrent(null);
      await qc.refetchQueries({ queryKey: ["companies"] });
      setShowDelete(false);
    },
  });

  const currentCompany =
    companies?.find(
      (company) =>
        company.id === currentId &&
        company.generation_id === currentGeneration,
    ) ?? null;

  return (
    <div className="flex items-center gap-2">
      <select
        aria-label="Select company"
        className="border border-slate-300 rounded px-2 py-1 text-sm bg-surface"
        value={currentId ?? ""}
        onChange={(e) => {
          const selected = companies?.find(
            (company) => company.id === e.target.value,
          );
          setCurrent(selected ?? null);
        }}
      >
        <option value="" disabled>
          {isLoading ? "Loading…" : "Select company"}
        </option>
        {companies?.map((c) => (
          <option key={`${c.id}:${c.generation_id}`} value={c.id}>
            {privacyOn ? maskName(c.name, "company") : c.name}
          </option>
        ))}
      </select>
      <button
        className="text-sm px-2 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700"
        onClick={() => setShowCreate(true)}
      >
        New Company
      </button>
      {currentCompany && (
        <button
          className="text-sm px-2 py-1 rounded border border-red-300 text-red-700 hover:bg-red-50"
          onClick={() => setShowDelete(true)}
          title="Delete the selected company"
        >
          Delete
        </button>
      )}
      {showCreate && (
        <CreateCompanyDialog
          onClose={() => setShowCreate(false)}
          onSubmit={(payload) => createMut.mutate(payload)}
          pending={createMut.isPending}
          error={createMut.error as Error | null}
        />
      )}
      {showDelete && currentCompany && (
        <DeleteCompanyDialog
          company={currentCompany}
          onClose={() => setShowDelete(false)}
          onConfirm={() => deleteMut.mutate(currentCompany.id)}
          pending={deleteMut.isPending}
          error={deleteMut.error as Error | null}
        />
      )}
    </div>
  );
}

function CreateCompanyDialog({
  onClose,
  onSubmit,
  pending,
  error,
}: {
  onClose: () => void;
  onSubmit: (payload: CompanyCreate) => void;
  pending: boolean;
  error: Error | null;
}) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [abn, setAbn] = useState("");
  const [gst, setGst] = useState(true);
  const submit = () => {
    if (pending || !id || !name) return;
    onSubmit({
      id,
      name,
      abn: abn || null,
      gst_registered: gst,
    });
  };

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-surface rounded-lg shadow-xl w-[420px] p-6">
        <h2 className="text-lg font-semibold mb-3">Create company</h2>
        <p className="text-xs text-slate-500 mb-4">
          A new SQLite file and default AU chart of accounts will be created locally.
        </p>
        <div className="space-y-3">
          <Field label="Short ID (folder name)">
            <input
              className="input"
              value={id}
              onChange={(e) => setId(e.target.value.toLowerCase())}
              placeholder="e.g. company_a"
              pattern="[a-z0-9][a-z0-9_-]*"
            />
          </Field>
          <Field label="Display name">
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Company A Pty Ltd"
            />
          </Field>
          <Field label="ABN (optional)">
            <input
              className="input"
              value={abn}
              onChange={(e) => setAbn(e.target.value)}
              placeholder="11 222 333 444"
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={gst} onChange={(e) => setGst(e.target.checked)} />
            GST registered
          </label>
        </div>
        {error && (
          <p className="text-sm text-red-600 mt-3">
            {apiErrorMessage(error)}
          </p>
        )}
        <div className="flex justify-end gap-2 mt-5">
          <button className="btn-secondary" onClick={onClose} disabled={pending}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={pending || !id || !name}
            onClick={submit}
          >
            {pending ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

function DeleteCompanyDialog({
  company,
  onClose,
  onConfirm,
  pending,
  error,
}: {
  company: Company;
  onClose: () => void;
  onConfirm: () => void;
  pending: boolean;
  error: Error | null;
}) {
  const [typed, setTyped] = useState("");
  // Require the user to type the company id — same intent as the backend's
  // ?confirm guard. A delete is irreversible (no recycle bin), so a single
  // click must not be enough.
  const matches = typed === company.id;

  const confirm = () => {
    if (pending || !matches) return;
    onConfirm();
  };

  useModalKeys({ open: true, onClose, onSubmit: confirm });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-surface rounded-lg shadow-xl w-[440px] p-6">
        <h2 className="text-lg font-semibold mb-1 text-red-700">Delete company</h2>
        <p className="text-sm text-slate-700 mb-3">
          This permanently deletes <strong>{company.name}</strong> (
          <code>{company.id}</code>) — its accounts, invoices, clients, cases,
          and all attachments. <strong>This cannot be undone.</strong>
        </p>
        <Field label={`Type the company id (${company.id}) to confirm`}>
          <input
            className="input"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={company.id}
            autoFocus
          />
        </Field>
        {error && (
          <p className="text-sm text-red-600 mt-3">{apiErrorMessage(error)}</p>
        )}
        <div className="flex justify-end gap-2 mt-5">
          <button className="btn-secondary" onClick={onClose} disabled={pending}>
            Cancel
          </button>
          <button
            className="text-sm px-3 py-1.5 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
            disabled={pending || !matches}
            onClick={confirm}
          >
            {pending ? "Deleting…" : "Delete permanently"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="block text-slate-600 mb-1">{label}</span>
      {children}
    </label>
  );
}
