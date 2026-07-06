import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import { displayName } from "../../lib/format";
import type { Client, ClientUpdate } from "../../types/api";

type Props = {
  client: Client;
  onClose: () => void;
};

export default function ClientDetailDrawer({ client, onClose }: Props) {
  const qc = useQueryClient();
  const [current, setCurrent] = useState(client);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<ClientUpdate>(() => clientToForm(client));
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    if (!editing) return;
    if (!form.display_name?.trim()) {
      setError("Name is required");
      return;
    }
    if (!mutation.isPending) mutation.mutate();
  };

  const mutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.patch<Client>(`/clients/${current.id}`, {
        display_name: form.display_name?.trim(),
        email: form.email?.trim() || null,
        phone: form.phone?.trim() || null,
        address: form.address?.trim() || null,
        client_ref: form.client_ref?.trim() || null,
        notes: form.notes?.trim() || null,
        is_active: form.is_active ?? true,
      });
      return data;
    },
    onSuccess: (updated) => {
      setCurrent(updated);
      setForm(clientToForm(updated));
      setEditing(false);
      setError(null);
      qc.invalidateQueries({ queryKey: ["clients"] });
    },
    onError: (e) => setError(apiErrorMessage(e)),
  });

  const cancelEdit = () => {
    setForm(clientToForm(current));
    setError(null);
    setEditing(false);
  };

  useModalKeys({ open: true, onClose, onSubmit: editing ? submit : undefined });

  return (
    <div className="fixed inset-0 z-40 flex">
      <div
        className="flex-1 bg-black/30"
        onClick={onClose}
        aria-label="Close drawer"
      />
      <aside className="w-[640px] bg-surface shadow-xl flex flex-col overflow-hidden">
        <header className="px-6 py-4 border-b flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold">{displayName(current.display_name, "client")}</h2>
            <div className="text-xs text-slate-500 mt-0.5">
              {current.client_ref ? `Ref: ${current.client_ref} · ` : ""}
              Client #{current.id}
              {!current.is_active && (
                <span className="ml-2 px-1.5 py-0.5 rounded bg-slate-200 text-slate-700">
                  inactive
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {!editing && (
              <button
                onClick={() => setEditing(true)}
                className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800"
              >
                Edit
              </button>
            )}
            <button
              onClick={onClose}
              className="text-slate-500 hover:text-slate-900"
            >
              ✕
            </button>
          </div>
        </header>

        <section className="flex-1 overflow-auto px-6 py-4 text-sm space-y-2">
          {editing ? (
            <form
              className="grid grid-cols-2 gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
            >
              <label className="text-xs col-span-2">
                Full name *
                <input
                  value={form.display_name ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, display_name: e.target.value })
                  }
                  className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
                  required
                />
              </label>
              <label className="text-xs">
                Email
                <input
                  type="email"
                  value={form.email ?? ""}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
                />
              </label>
              <label className="text-xs">
                Phone
                <input
                  value={form.phone ?? ""}
                  onChange={(e) => setForm({ ...form, phone: e.target.value })}
                  className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
                />
              </label>
              <label className="text-xs col-span-2">
                Address
                <input
                  value={form.address ?? ""}
                  onChange={(e) => setForm({ ...form, address: e.target.value })}
                  className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
                />
              </label>
              <label className="text-xs">
                Internal ref (e.g. JS-001)
                <input
                  value={form.client_ref ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, client_ref: e.target.value })
                  }
                  className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
                />
              </label>
              <label className="text-xs flex items-center gap-2 self-end pb-1">
                <input
                  type="checkbox"
                  checked={form.is_active ?? true}
                  onChange={(e) =>
                    setForm({ ...form, is_active: e.target.checked })
                  }
                />
                Active
              </label>
              <label className="text-xs col-span-2">
                Notes
                <textarea
                  rows={3}
                  value={form.notes ?? ""}
                  onChange={(e) => setForm({ ...form, notes: e.target.value })}
                  className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
                />
              </label>
              {error && (
                <div className="col-span-2 text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded px-2 py-1">
                  {error}
                </div>
              )}
              <div className="col-span-2 flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={cancelEdit}
                  className="px-3 py-1.5 text-sm border rounded"
                  disabled={mutation.isPending}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={mutation.isPending}
                  className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white disabled:opacity-60"
                >
                  {mutation.isPending ? "Saving…" : "Save"}
                </button>
              </div>
            </form>
          ) : (
            <>
              {current.email && (
                <div>
                  <span className="text-xs uppercase tracking-wide text-slate-500">Email</span>
                  <div>{current.email}</div>
                </div>
              )}
              {current.phone && (
                <div>
                  <span className="text-xs uppercase tracking-wide text-slate-500">Phone</span>
                  <div>{current.phone}</div>
                </div>
              )}
              {current.address && (
                <div>
                  <span className="text-xs uppercase tracking-wide text-slate-500">Address</span>
                  <div>{current.address}</div>
                </div>
              )}
              {current.notes && (
                <div>
                  <span className="text-xs uppercase tracking-wide text-slate-500">Notes</span>
                  <div className="whitespace-pre-wrap">{current.notes}</div>
                </div>
              )}
            </>
          )}
        </section>
      </aside>
    </div>
  );
}

function clientToForm(client: Client): ClientUpdate {
  return {
    display_name: client.display_name,
    email: client.email ?? "",
    phone: client.phone ?? "",
    address: client.address ?? "",
    client_ref: client.client_ref ?? "",
    notes: client.notes ?? "",
    is_active: client.is_active,
  };
}
