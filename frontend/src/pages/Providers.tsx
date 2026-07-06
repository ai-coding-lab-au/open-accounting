import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useCompanyStore } from "../store/company";
import { useModalKeys } from "../lib/useModalKeys";
import { apiErrorMessage } from "../lib/errors";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import type { Contact, ContactCreate, ContactUpdate } from "../types/api";
import { displayEmail, displayId, displayName, displayPhone } from "../lib/format";

const KIND_LABEL: Record<Contact["kind"], string> = {
  supplier: "Supplier",
  customer: "Customer",
  both: "Both",
};

async function fetchContacts(params: { q?: string; kind?: string }): Promise<Contact[]> {
  const { data } = await api.get<Contact[]>("/contacts", { params });
  return data;
}

async function createContact(payload: ContactCreate): Promise<Contact> {
  const { data } = await api.post<Contact>("/contacts", payload);
  return data;
}

async function updateContact(id: number, payload: ContactUpdate): Promise<Contact> {
  const { data } = await api.patch<Contact>(`/contacts/${id}`, payload);
  return data;
}

async function deleteContact(id: number): Promise<void> {
  await api.delete(`/contacts/${id}`);
}

export default function ProvidersPage() {
  const currentId = useCompanyStore((s) => s.currentId);
  const qc = useQueryClient();

  const [q, setQ] = useState("");
  const [kindFilter, setKindFilter] = useState<"all" | "supplier" | "customer" | "both">(
    "all",
  );
  const [editing, setEditing] = useState<Contact | null>(null);
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Contact | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["contacts", currentId, q, kindFilter],
    queryFn: () =>
      fetchContacts({
        q: q.trim() || undefined,
        kind: kindFilter === "all" ? undefined : kindFilter,
      }),
    enabled: !!currentId,
  });

  const remove = useMutation({
    mutationFn: (c: Contact) => deleteContact(c.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["contacts"] }),
  });

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">Pick a company in the top bar.</p>
      </div>
    );
  }

  const counts = useMemo(() => {
    const s = { total: data?.length ?? 0, supplier: 0, customer: 0, both: 0 };
    for (const c of data ?? []) {
      if (c.kind === "supplier") s.supplier++;
      else if (c.kind === "customer") s.customer++;
      else s.both++;
    }
    return s;
  }, [data]);

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">Providers</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Suppliers we pay (AP). Customers we serve live in{" "}
            <Link to="/clients" className="underline">
              Clients
            </Link>
            .
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by name…"
            className="border rounded px-2 py-1 text-sm w-56"
          />
          <select
            value={kindFilter}
            onChange={(e) =>
              setKindFilter(e.target.value as typeof kindFilter)
            }
            className="border rounded px-2 py-1 text-sm"
          >
            <option value="all">All kinds</option>
            <option value="supplier">Suppliers only</option>
            <option value="customer">Customers only</option>
            <option value="both">Both</option>
          </select>
          <button
            onClick={() => setCreating(true)}
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800"
          >
            + New Provider
          </button>
        </div>
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        <div className="px-4 py-3 border-b bg-slate-50 flex justify-between text-sm">
          <span>
            <strong>{counts.total}</strong> shown · {counts.supplier} supplier ·{" "}
            {counts.customer} customer · {counts.both} both
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
                <th className="py-2 px-3">Name</th>
                <th className="py-2 px-3 w-28">Kind</th>
                <th className="py-2 px-3 w-32">ABN</th>
                <th className="py-2 px-3">Email / Phone</th>
                <th className="py-2 px-3 w-32 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((c) => (
                <tr
                  key={c.id}
                  className={`border-b last:border-b-0 ${c.active ? "" : "bg-slate-50 text-slate-400"}`}
                >
                  <td className="py-1.5 px-3">
                    <div>
                      {displayName(c.name, "provider")}
                      {!c.active && (
                        <span className="ml-2 text-[10px] uppercase text-slate-400">inactive</span>
                      )}
                    </div>
                    {c.notes && (
                      <div className="text-xs text-slate-500">{c.notes}</div>
                    )}
                  </td>
                  <td className="py-1.5 px-3">
                    <span className="text-xs px-2 py-0.5 rounded bg-slate-100 border border-slate-200">
                      {KIND_LABEL[c.kind]}
                    </span>
                  </td>
                  <td className="py-1.5 px-3 font-mono text-xs">{displayId(c.abn, "abn")}</td>
                  <td className="py-1.5 px-3 text-xs text-slate-600">
                    <div>{displayEmail(c.email)}</div>
                    <div>{displayPhone(c.phone)}</div>
                  </td>
                  <td className="py-1.5 px-3 text-right space-x-2">
                    <button
                      onClick={() => setEditing(c)}
                      className="text-xs text-slate-600 hover:text-slate-900 underline"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => {
                        setDeleteError(null);
                        setPendingDelete(c);
                      }}
                      disabled={remove.isPending}
                      className="text-xs text-rose-600 hover:text-rose-800 underline"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
              {(data ?? []).length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-500">
                    No providers match these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <ProviderFormDialog
          existing={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["contacts"] });
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        destructive
        title="Delete provider?"
        message={
          pendingDelete
            ? `Delete provider "${displayName(pendingDelete.name, "provider")}"? This only works if they have no invoices.`
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

function ProviderFormDialog({
  existing,
  onClose,
  onSaved,
}: {
  existing: Contact | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = !!existing;
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<Contact["kind"]>(existing?.kind ?? "supplier");
  const [abn, setAbn] = useState(existing?.abn ?? "");
  const [email, setEmail] = useState(existing?.email ?? "");
  const [phone, setPhone] = useState(existing?.phone ?? "");
  const [address, setAddress] = useState(existing?.address ?? "");
  const [notes, setNotes] = useState(existing?.notes ?? "");
  const [active, setActive] = useState(existing?.active ?? true);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const submit = () => {
    setSubmitError(null);
    if (!name.trim()) {
      setSubmitError("Name is required.");
      return;
    }
    save.mutate();
  };

  const save = useMutation({
    mutationFn: async () => {
      const base = {
        name: name.trim(),
        kind,
        abn: abn.trim() || null,
        email: email.trim() || null,
        phone: phone.trim() || null,
        address: address.trim() || null,
        notes: notes.trim() || null,
        active,
      };
      return isEdit
        ? updateContact(existing!.id, base)
        : createContact(base as ContactCreate);
    },
    onSuccess: () => onSaved(),
    onError: (e) => {
      setSubmitError(apiErrorMessage(e));
    },
  });

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 z-40 bg-slate-900/30 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl w-full max-w-lg">
        <div className="px-5 py-3 border-b flex justify-between items-center">
          <h3 className="font-semibold">
            {isEdit ? `Edit ${displayName(existing!.name, "provider")}` : "New provider"}
          </h3>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-900">
            ✕
          </button>
        </div>
        <div className="p-5 space-y-3 text-sm">
          <Field label="Name">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="border rounded px-2 py-1 w-full"
            />
          </Field>
          <Field label="Kind">
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as Contact["kind"])}
              className="border rounded px-2 py-1 w-full"
            >
              <option value="supplier">Supplier (we pay them)</option>
              <option value="customer">Customer (legacy — prefer Clients)</option>
              <option value="both">Both</option>
            </select>
          </Field>
          <Field label="ABN (optional)">
            <input
              value={abn}
              onChange={(e) => setAbn(e.target.value)}
              className="border rounded px-2 py-1 w-full font-mono"
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Email">
              <input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="border rounded px-2 py-1 w-full"
              />
            </Field>
            <Field label="Phone">
              <input
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                className="border rounded px-2 py-1 w-full"
              />
            </Field>
          </div>
          <Field label="Address">
            <textarea
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              rows={2}
              placeholder="Printed as the Bill To address on invoices / receipts"
              className="border rounded px-2 py-1 w-full"
            />
          </Field>
          <Field label="Notes">
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              className="border rounded px-2 py-1 w-full"
            />
          </Field>
          {isEdit && (
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={active}
                onChange={(e) => setActive(e.target.checked)}
              />
              <span>Active (inactive providers are hidden from pickers)</span>
            </label>
          )}
          {submitError && (
            <div className="text-rose-700 text-xs">{submitError}</div>
          )}
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
            disabled={save.isPending}
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {save.isPending ? "Saving…" : isEdit ? "Save changes" : "Create"}
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
