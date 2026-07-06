import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import type { Client } from "../../types/api";

async function fetchClients(q: string): Promise<Client[]> {
  const { data } = await api.get<Client[]>("/clients", {
    params: { q: q.trim() || undefined, active_only: true },
  });
  return data;
}

export default function ClientSelect({
  selectedId,
  selectedName,
  onPick,
  label = "Client",
}: {
  selectedId: number | null;
  selectedName: string;
  onPick: (client: Client | null) => void;
  label?: string;
}) {
  const currentId = useCompanyStore((s) => s.currentId);
  const [search, setSearch] = useState(selectedName);
  const [open, setOpen] = useState(false);
  const [debounced, setDebounced] = useState(search);

  useEffect(() => {
    if (!open) setSearch(selectedName);
  }, [open, selectedName]);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search), 200);
    return () => clearTimeout(t);
  }, [search]);

  const { data: matches } = useQuery({
    queryKey: ["clients-search", currentId, debounced],
    queryFn: () => fetchClients(debounced),
    enabled: open && !!currentId,
  });

  const createHref = useMemo(() => {
    const params = new URLSearchParams({ new: "1" });
    const name = search.trim() || selectedName.trim();
    if (name) params.set("name", name);
    return `/clients?${params.toString()}`;
  }, [search, selectedName]);

  return (
    <div className="space-y-2">
      <label className="block">
        <span className="block text-xs font-medium text-slate-600 mb-1">{label}</span>
        <div className="grid grid-cols-[1fr_auto] gap-2">
          <input
            className="input"
            value={open ? search : selectedName}
            onFocus={() => {
              setOpen(true);
              setSearch(selectedName);
            }}
            onChange={(e) => {
              setSearch(e.target.value);
              if (selectedId) onPick(null);
              setOpen(true);
            }}
            placeholder="Search clients…"
          />
          {selectedId ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                onPick(null);
                setSearch("");
                setOpen(true);
              }}
            >
              Clear
            </button>
          ) : (
            <Link className="btn-secondary whitespace-nowrap" to={createHref}>
              + New Client
            </Link>
          )}
        </div>
      </label>

      {open && (
        <div className="relative">
          <div className="absolute z-20 bg-surface border border-slate-300 rounded shadow-lg w-full max-h-64 overflow-auto">
            {matches && matches.length > 0 ? (
              matches.slice(0, 12).map((client) => (
                <button
                  key={client.id}
                  type="button"
                  className="block w-full text-left px-3 py-1.5 text-sm hover:bg-slate-100"
                  onClick={() => {
                    onPick(client);
                    setOpen(false);
                  }}
                >
                  <div className="font-medium">{client.display_name}</div>
                  <div className="text-xs text-slate-500">
                    {[client.email, client.phone].filter(Boolean).join(" · ") || "No contact details"}
                  </div>
                </button>
              ))
            ) : (
              <div className="px-3 py-2 text-xs text-slate-500">No matching clients.</div>
            )}
            <div className="border-t bg-slate-50 flex items-center justify-between gap-2 px-3 py-2">
              <span className="text-xs text-slate-500">Client not listed?</span>
              <Link className="text-sm text-emerald-700 hover:underline" to={createHref}>
                Create in Clients
              </Link>
            </div>
          </div>
        </div>
      )}

      <div className="text-xs text-slate-500 flex items-center gap-2">
        {selectedId ? (
          <span className="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-800">
            Linked to client #{selectedId}
          </span>
        ) : (
          <span className="text-amber-700">Select an existing client before creating this document.</span>
        )}
        {open && (
          <button
            type="button"
            className="ml-auto text-slate-500 hover:text-slate-900"
            onClick={() => setOpen(false)}
          >
            close picker
          </button>
        )}
      </div>
    </div>
  );
}
