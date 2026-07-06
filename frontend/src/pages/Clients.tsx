import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { apiErrorMessage } from "../lib/errors";
import { useModalKeys } from "../lib/useModalKeys";
import { useCompanyStore } from "../store/company";
import { displayEmail, displayName, displayPhone } from "../lib/format";
import type { Client, ClientCreate } from "../types/api";
import ClientDetailDrawer from "../components/clients/ClientDetailDrawer";

async function fetchClients(params: {
  q?: string;
  active_only?: boolean;
}): Promise<Client[]> {
  const { data } = await api.get<Client[]>("/clients", { params });
  return data;
}

async function createClient(payload: ClientCreate) {
  const { data } = await api.post<Client>("/clients", payload);
  return data;
}

export default function ClientsPage() {
  const currentId = useCompanyStore((s) => s.currentId);
  const [q, setQ] = useState("");
  const [activeOnly, setActiveOnly] = useState(true);
  const [selected, setSelected] = useState<Client | null>(null);
  const [creating, setCreating] = useState(false);
  const [createSeedName, setCreateSeedName] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();

  const qc = useQueryClient();

  useEffect(() => {
    if (searchParams.get("new") !== "1") return;
    setCreateSeedName(searchParams.get("name") ?? "");
    setCreating(true);
  }, [searchParams]);
  const { data, isLoading, error } = useQuery({
    queryKey: ["clients", currentId, q, activeOnly],
    queryFn: () =>
      fetchClients({
        q: q.trim() || undefined,
        active_only: activeOnly,
      }),
    enabled: !!currentId,
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

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">Clients</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            People &amp; entities we provide services to.
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by name…"
            className="border rounded px-2 py-1 text-sm w-56"
          />
          <label className="text-xs flex items-center gap-1">
            <input
              type="checkbox"
              checked={activeOnly}
              onChange={(e) => setActiveOnly(e.target.checked)}
            />
            Active only
          </label>
          <button
            onClick={() => {
              setCreateSeedName("");
              setCreating(true);
            }}
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800"
          >
            + New Client
          </button>
        </div>
      </div>

      <div className="bg-surface rounded-lg border border-slate-200 overflow-hidden">
        <div className="px-4 py-3 border-b bg-slate-50 text-sm">
          <span>
            <strong>{data?.length ?? 0}</strong> clients
          </span>
        </div>
        {isLoading ? (
          <div className="px-4 py-6 text-sm text-slate-500">Loading…</div>
        ) : error ? (
          <div className="px-4 py-6 text-sm text-rose-700">
            Failed to load clients.
          </div>
        ) : !data || data.length === 0 ? (
          <div className="px-4 py-6 text-sm text-slate-500">
            No clients yet. Click <strong>+ New Client</strong> to add one.
          </div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
            <thead className="text-xs text-slate-600 bg-slate-50">
              <tr>
                <th className="text-left px-4 py-2">Name</th>
                <th className="text-left px-4 py-2">Ref</th>
                <th className="text-left px-4 py-2">Email</th>
                <th className="text-left px-4 py-2">Phone</th>
              </tr>
            </thead>
            <tbody>
              {data.map((c) => (
                <tr
                  key={c.id}
                  onClick={() => setSelected(c)}
                  className="border-t hover:bg-slate-50 cursor-pointer"
                >
                  <td className="px-4 py-2">
                    <div className="font-medium">{displayName(c.display_name, "client")}</div>
                    {!c.is_active && (
                      <span className="text-[10px] px-1 py-0.5 rounded bg-slate-200 text-slate-700">
                        inactive
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-slate-500">
                    {c.client_ref ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-xs">{displayEmail(c.email)}</td>
                  <td className="px-4 py-2 text-xs">{displayPhone(c.phone)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

      {selected && (
        <ClientDetailDrawer
          client={selected}
          onClose={() => setSelected(null)}
        />
      )}

      {creating && (
        <CreateClientDialog
          existingRefs={(data ?? []).map((c) => c.client_ref).filter((r): r is string => !!r)}
          seedName={createSeedName}
          onClose={() => {
            setCreating(false);
            setSearchParams({});
          }}
          onCreated={() => {
            setCreating(false);
            setSearchParams({});
            qc.invalidateQueries({ queryKey: ["clients"] });
          }}
        />
      )}
    </div>
  );
}

function nextClientRef(existing: string[]): string {
  // Find the largest <prefix>-<number> pattern and increment. Preserve prefix
  // and zero-padding width so JS-006 → JS-007, CLIENT-042 → CLIENT-043.
  let best: { prefix: string; width: number; num: number } | null = null;
  for (const r of existing) {
    const m = /^([A-Za-z]+)-?(\d+)$/.exec(r.trim());
    if (!m) continue;
    const [, prefix, digits] = m;
    const num = parseInt(digits, 10);
    if (!best || num > best.num) {
      best = { prefix, width: digits.length, num };
    }
  }
  if (!best) return "";
  const next = String(best.num + 1).padStart(best.width, "0");
  return `${best.prefix}-${next}`;
}

function CreateClientDialog({
  existingRefs,
  seedName,
  onClose,
  onCreated,
}: {
  existingRefs: string[];
  seedName?: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [form, setForm] = useState<ClientCreate>(() => ({
    display_name: seedName ?? "",
    email: "",
    phone: "",
    address: "",
    client_ref: nextClientRef(existingRefs),
    notes: "",
  }));
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    if (!form.display_name.trim()) {
      setError("Name is required");
      return;
    }
    if (!mutation.isPending) mutation.mutate();
  };

  const mutation = useMutation({
    mutationFn: () =>
      createClient({
        display_name: form.display_name.trim(),
        email: form.email?.trim() || null,
        phone: form.phone?.trim() || null,
        address: form.address?.trim() || null,
        client_ref: form.client_ref?.trim() || null,
        notes: form.notes?.trim() || null,
      }),
    onSuccess: () => onCreated(),
    onError: (e) => setError(apiErrorMessage(e)),
  });

  useModalKeys({ open: true, onClose, onSubmit: submit });

  return (
    <div className="fixed inset-0 z-40 bg-black/30 flex items-center justify-center p-6">
      <div className="bg-surface rounded-lg shadow-xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-semibold mb-3">New client</h2>
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
              value={form.display_name}
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
              onChange={(e) => setForm({ ...form, client_ref: e.target.value })}
              className="mt-0.5 w-full border rounded px-2 py-1 text-sm"
            />
          </label>
          <label className="text-xs col-span-2">
            Notes
            <textarea
              rows={2}
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
              onClick={onClose}
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
              {mutation.isPending ? "Saving…" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
