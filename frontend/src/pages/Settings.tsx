import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { apiErrorMessage } from "../lib/errors";
import { useCompanyStore } from "../store/company";
import { ThemePickerGrid } from "../components/ThemePicker";
import type { Company, CompanyUpdate, DocCounter, OutgoingDocType } from "../types/api";

async function fetchCompany(id: string): Promise<Company> {
  const { data } = await api.get<Company>(`/companies/${id}`);
  return data;
}

async function patchCompany(id: string, payload: CompanyUpdate): Promise<Company> {
  const { data } = await api.patch<Company>(`/companies/${id}`, payload);
  return data;
}

async function fetchCounters(): Promise<DocCounter[]> {
  const { data } = await api.get<DocCounter[]>("/outgoing/counters");
  return data;
}

type CounterDocType = OutgoingDocType;

async function setCounter(payload: {
  doc_type: CounterDocType;
  year: number;
  last_number: number;
}): Promise<DocCounter> {
  const { data } = await api.put<DocCounter>("/outgoing/counters", payload);
  return data;
}

export default function SettingsPage() {
  const currentId = useCompanyStore((s) => s.currentId);

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">Pick a company in the top bar.</p>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <h1 className="text-xl font-semibold">Settings</h1>
      <CompanyProfileCard companyId={currentId} />
      <BankDetailsCard companyId={currentId} />
      <NumberingCard />
      <StaffCard />
      <AppearanceCard />
    </div>
  );
}

function AppearanceCard() {
  return (
    <Card
      title="Appearance"
      subtitle="Pick a theme. Click any swatch to preview live — choice is saved locally and applied across all pages."
    >
      <ThemePickerGrid />
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Company profile (used for the top-left header block of outgoing documents)
// ---------------------------------------------------------------------------

function CompanyProfileCard({ companyId }: { companyId: string }) {
  const qc = useQueryClient();
  const { data: company } = useQuery({
    queryKey: ["company", companyId],
    queryFn: () => fetchCompany(companyId),
  });

  const [form, setForm] = useState<CompanyUpdate>({});
  useEffect(() => {
    if (company) setForm({});
  }, [company?.id]);

  const mut = useMutation({
    mutationFn: (payload: CompanyUpdate) => patchCompany(companyId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["company", companyId] });
      qc.invalidateQueries({ queryKey: ["companies"] });
      setForm({});
    },
  });

  if (!company) return <Card title="Company profile">Loading…</Card>;

  const merged: Partial<Company> = { ...company, ...form };
  const set = <K extends keyof CompanyUpdate>(k: K, v: CompanyUpdate[K]) =>
    setForm((f) => ({ ...f, [k]: v }));
  const dirty = Object.keys(form).length > 0;

  return (
    <Card
      title="Company profile"
      subtitle="Printed in the top-left of every Receipt."
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Display name">
          <input
            className="input"
            value={merged.name ?? ""}
            onChange={(e) => set("name", e.target.value)}
          />
        </Field>
        <Field label="Legal name (optional)">
          <input
            className="input"
            value={merged.legal_name ?? ""}
            onChange={(e) => set("legal_name", e.target.value)}
          />
        </Field>
        <Field label="ABN (optional)">
          <input
            className="input"
            value={merged.abn ?? ""}
            onChange={(e) => set("abn", e.target.value)}
          />
        </Field>
        <Field label="Phone">
          <input
            className="input"
            value={merged.phone ?? ""}
            onChange={(e) => set("phone", e.target.value)}
          />
        </Field>
        <Field label="Email">
          <input
            className="input"
            value={merged.email ?? ""}
            onChange={(e) => set("email", e.target.value)}
          />
        </Field>
        <Field label="Website (optional)">
          <input
            className="input"
            value={merged.website ?? ""}
            onChange={(e) => set("website", e.target.value)}
          />
        </Field>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <Field label="Address line 1">
          <input
            className="input"
            value={merged.address_line1 ?? ""}
            onChange={(e) => set("address_line1", e.target.value)}
          />
        </Field>
        <Field label="Address line 2 (optional)">
          <input
            className="input"
            value={merged.address_line2 ?? ""}
            onChange={(e) => set("address_line2", e.target.value)}
          />
        </Field>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-3">
        <Field label="Suburb">
          <input
            className="input"
            value={merged.suburb ?? ""}
            onChange={(e) => set("suburb", e.target.value)}
          />
        </Field>
        <Field label="State">
          <input
            className="input"
            value={merged.state ?? ""}
            onChange={(e) => set("state", e.target.value)}
          />
        </Field>
        <Field label="Postcode">
          <input
            className="input"
            value={merged.postcode ?? ""}
            onChange={(e) => set("postcode", e.target.value)}
          />
        </Field>
      </div>


      <label className="flex items-center gap-2 text-sm mt-4">
        <input
          type="checkbox"
          checked={merged.gst_registered ?? false}
          onChange={(e) => set("gst_registered", e.target.checked)}
        />
        GST registered (affects whether the PDF shows a GST line and the "Tax Invoice" wording)
      </label>

      <label className="flex items-center gap-2 text-sm mt-2">
        <input
          type="checkbox"
          checked={merged.bilingual_labels ?? false}
          onChange={(e) => set("bilingual_labels", e.target.checked)}
        />
        Bilingual document labels 中英双语单据标签 (receipt PDFs show "RECEIPT 收据",
        "TOTAL 总计" etc. — your own text is never translated)
      </label>

      <div className="mt-4 flex justify-end gap-2">
        {mut.isError && (
          <p className="text-sm text-red-600 mr-auto">
            {apiErrorMessage(mut.error)}
          </p>
        )}
        <button
          className="btn-secondary"
          disabled={!dirty || mut.isPending}
          onClick={() => setForm({})}
        >
          Discard
        </button>
        <button
          className="btn-primary"
          disabled={!dirty || mut.isPending}
          onClick={() => mut.mutate(form)}
        >
          {mut.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Bank / payment details (printed in PAYMENT METHOD block)
// ---------------------------------------------------------------------------

function BankDetailsCard({ companyId }: { companyId: string }) {
  const qc = useQueryClient();
  const { data: company } = useQuery({
    queryKey: ["company", companyId],
    queryFn: () => fetchCompany(companyId),
  });
  const [form, setForm] = useState<CompanyUpdate>({});
  useEffect(() => {
    if (company) setForm({});
  }, [company?.id]);
  const mut = useMutation({
    mutationFn: (payload: CompanyUpdate) => patchCompany(companyId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["company", companyId] });
      setForm({});
    },
  });

  if (!company) return <Card title="Bank details">Loading…</Card>;
  const merged: Partial<Company> = { ...company, ...form };
  const set = <K extends keyof CompanyUpdate>(k: K, v: CompanyUpdate[K]) =>
    setForm((f) => ({ ...f, [k]: v }));
  const dirty = Object.keys(form).length > 0;

  return (
    <Card
      title="Bank details"
      subtitle="Your receiving account — printed on receipts so clients know where they paid."
    >
      <h3 className="text-sm font-semibold text-slate-700 mb-2">
        Receiving account
        <span className="font-normal text-slate-500"> — prints on receipts</span>
      </h3>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Account name">
          <input
            className="input"
            value={merged.bank_account_name ?? ""}
            onChange={(e) => set("bank_account_name", e.target.value)}
          />
        </Field>
        <Field label="Bank">
          <input
            className="input"
            value={merged.bank_name ?? ""}
            onChange={(e) => set("bank_name", e.target.value)}
          />
        </Field>
        <Field label="BSB">
          <input
            className="input"
            value={merged.bank_bsb ?? ""}
            onChange={(e) => set("bank_bsb", e.target.value)}
          />
        </Field>
        <Field label="Account number">
          <input
            className="input"
            value={merged.bank_account_number ?? ""}
            onChange={(e) => set("bank_account_number", e.target.value)}
          />
        </Field>
        <Field label="SWIFT code (for overseas transfers)">
          <input
            className="input"
            value={merged.bank_swift ?? ""}
            onChange={(e) => set("bank_swift", e.target.value)}
          />
        </Field>
      </div>
      <h3 className="text-sm font-semibold text-slate-700 mt-6 mb-2 pt-4 border-t border-slate-200">
        Secondary account
        <span className="font-normal text-slate-500"> — optional; not printed</span>
      </h3>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Account name">
          <input
            className="input"
            value={merged.operating_bank_account_name ?? ""}
            onChange={(e) => set("operating_bank_account_name", e.target.value)}
          />
        </Field>
        <Field label="Bank">
          <input
            className="input"
            value={merged.operating_bank_name ?? ""}
            onChange={(e) => set("operating_bank_name", e.target.value)}
          />
        </Field>
        <Field label="BSB">
          <input
            className="input"
            value={merged.operating_bank_bsb ?? ""}
            onChange={(e) => set("operating_bank_bsb", e.target.value)}
          />
        </Field>
        <Field label="Account number">
          <input
            className="input"
            value={merged.operating_bank_account_number ?? ""}
            onChange={(e) => set("operating_bank_account_number", e.target.value)}
          />
        </Field>
        <Field label="SWIFT code (for overseas transfers)">
          <input
            className="input"
            value={merged.operating_bank_swift ?? ""}
            onChange={(e) => set("operating_bank_swift", e.target.value)}
          />
        </Field>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        {mut.isError && (
          <p className="text-sm text-red-600 mr-auto">
            {apiErrorMessage(mut.error)}
          </p>
        )}
        <button
          className="btn-secondary"
          disabled={!dirty || mut.isPending}
          onClick={() => setForm({})}
        >
          Discard
        </button>
        <button
          className="btn-primary"
          disabled={!dirty || mut.isPending}
          onClick={() => mut.mutate(form)}
        >
          {mut.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// Document defaults
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Document numbering counters
// ---------------------------------------------------------------------------

function NumberingCard() {
  const qc = useQueryClient();
  const currentId = useCompanyStore((s) => s.currentId);
  const { data: counters } = useQuery({
    queryKey: ["counters", currentId],
    queryFn: fetchCounters,
    enabled: !!currentId,
  });
  const mut = useMutation({
    mutationFn: setCounter,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["counters", currentId] }),
  });

  const year = new Date().getFullYear();
  const currentYearCounters = (counters ?? []).filter((c) => c.year === year);
  const lastNumber = Math.max(0, ...currentYearCounters.map((c) => c.last_number));

  return (
    <Card
      title="Document numbering"
      subtitle="Set the last-used number for Receipts."
    >
      <UnifiedCounterRow
        year={year}
        lastNumber={lastNumber}
        pending={mut.isPending}
        onSave={(n) =>
          mut.mutate({ doc_type: "receipt", year, last_number: n })
        }
      />
    </Card>
  );
}

function UnifiedCounterRow({
  year,
  lastNumber,
  pending,
  onSave,
}: {
  year: number;
  lastNumber: number;
  pending: boolean;
  onSave: (n: number) => void;
}) {
  const [valueText, setValueText] = useState(String(lastNumber));
  useEffect(() => setValueText(String(lastNumber)), [lastNumber]);
  const value = Number(valueText || 0);
  const dirty = value !== lastNumber;
  const nextSerial = String(value + 1).padStart(4, "0");
  const previewLocal = `RCT-${year}-${nextSerial}`;

  return (
    <div className="grid gap-4 md:grid-cols-[160px_220px_1fr_auto] md:items-end text-sm">
      <Field label="Year">
        <div className="input flex items-center bg-slate-50">{year}</div>
      </Field>
      <Field label="Shared last used #">
        <input
          type="number"
          min={0}
          step={1}
          className="input"
          value={valueText}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              setValueText("");
              return;
            }
            const parsed = Math.max(0, Math.trunc(Number(raw)));
            if (Number.isFinite(parsed)) setValueText(String(parsed));
          }}
          onBlur={() => setValueText(String(value))}
        />
      </Field>
      <Field label="Next numbers">
        <div className="input flex items-center bg-slate-50 font-mono text-slate-600 overflow-x-auto whitespace-nowrap">
          {previewLocal}
        </div>
      </Field>
      <button
        className="btn-primary h-[42px]"
        disabled={!dirty || pending}
        onClick={() => onSave(value)}
      >
        {pending ? "Saving…" : "Save"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="bg-surface rounded-md border border-slate-200 p-5">
      <h2 className="font-semibold">{title}</h2>
      {subtitle && <p className="text-xs text-slate-500 mt-0.5 mb-3">{subtitle}</p>}
      {children}
    </section>
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

// ---------------------------------------------------------------------------
// Staff list editor (document signers)
// ---------------------------------------------------------------------------

type StaffRegType = "mara" | "lpn" | "none";

interface Staff {
  id: number;
  full_name: string;
  registration_type: StaffRegType;
  registration_number: string | null;
  active: boolean;
  display_label: string;
}

interface StaffPayload {
  full_name: string;
  registration_type: StaffRegType;
  registration_number: string | null;
  active: boolean;
}

const REG_LABELS: Record<StaffRegType, string> = {
  mara: "MARA (MARN)",
  lpn: "Legal practitioner (LPN)",
  none: "No registration",
};

function StaffCard() {
  const qc = useQueryClient();
  const currentId = useCompanyStore((s) => s.currentId);
  const { data: staff, isLoading } = useQuery({
    // Scope by company id so a company switch never shows another company's
    // staff rows (whose ids would otherwise be mutated under the new header).
    queryKey: ["staff", currentId],
    queryFn: async () => (await api.get<Staff[]>("/staff")).data,
  });
  const createMut = useMutation({
    mutationFn: async (p: StaffPayload) => (await api.post<Staff>("/staff", p)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["staff", currentId] }),
  });
  const updateMut = useMutation({
    mutationFn: async ({ id, ...p }: StaffPayload & { id: number }) =>
      (await api.put<Staff>(`/staff/${id}`, p)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["staff", currentId] }),
  });
  const deleteMut = useMutation({
    mutationFn: async (id: number) => api.delete(`/staff/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["staff", currentId] }),
  });
  const [showNew, setShowNew] = useState(false);
  const err = createMut.error || updateMut.error || deleteMut.error;

  return (
    <Card
      title="Staff"
      subtitle="People selectable as the signing staff member on documents. Registration may be MARA (MARN), legal practitioner (LPN), or none."
    >
      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {staff && staff.length === 0 && !showNew && (
        <p className="text-sm text-slate-500 italic">
          No staff yet. Click <strong>+ New staff</strong> to add one.
        </p>
      )}
      {staff && staff.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-500 border-b border-slate-200">
                <th className="py-2 pr-3">Name</th>
                <th className="py-2 pr-3">Registration</th>
                <th className="py-2 pr-3">Number</th>
                <th className="py-2 pr-3"></th>
              </tr>
            </thead>
            <tbody>
              {staff.map((row) => (
                <StaffRow
                  key={row.id}
                  row={row}
                  onSave={(p) => updateMut.mutate({ id: row.id, ...p })}
                  onDeactivate={() => deleteMut.mutate(row.id)}
                  pending={updateMut.isPending || deleteMut.isPending}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-3 flex items-center gap-2">
        {!showNew && (
          <button className="btn-secondary" onClick={() => setShowNew(true)}>
            + New staff
          </button>
        )}
        {showNew && (
          <StaffForm
            onSave={(p) => createMut.mutate(p, { onSuccess: () => setShowNew(false) })}
            onCancel={() => setShowNew(false)}
            pending={createMut.isPending}
          />
        )}
        {err && <p className="text-sm text-rose-600 ml-3">{apiErrorMessage(err)}</p>}
      </div>
    </Card>
  );
}

function StaffRow({
  row,
  onSave,
  onDeactivate,
  pending,
}: {
  row: Staff;
  onSave: (p: StaffPayload) => void;
  onDeactivate: () => void;
  pending: boolean;
}) {
  const [editing, setEditing] = useState(false);
  if (editing) {
    return (
      <tr className="border-b border-slate-100">
        <td colSpan={4} className="py-2">
          <StaffForm
            initial={row}
            onSave={(p) => {
              onSave(p);
              setEditing(false);
            }}
            onCancel={() => setEditing(false)}
            pending={pending}
          />
        </td>
      </tr>
    );
  }
  return (
    <tr className="border-b border-slate-100">
      <td className="py-2 pr-3">{row.full_name}</td>
      <td className="py-2 pr-3">{REG_LABELS[row.registration_type]}</td>
      <td className="py-2 pr-3">{row.registration_number ?? "—"}</td>
      <td className="py-2 pr-3 text-right whitespace-nowrap">
        <button className="text-xs text-amber-700 mr-3" onClick={() => setEditing(true)}>
          Edit
        </button>
        <button className="text-xs text-rose-700" disabled={pending} onClick={onDeactivate}>
          Remove
        </button>
      </td>
    </tr>
  );
}

function StaffForm({
  initial,
  onSave,
  onCancel,
  pending,
}: {
  initial?: Staff;
  onSave: (p: StaffPayload) => void;
  onCancel: () => void;
  pending: boolean;
}) {
  const [name, setName] = useState(initial?.full_name ?? "");
  const [regType, setRegType] = useState<StaffRegType>(initial?.registration_type ?? "none");
  const [number, setNumber] = useState(initial?.registration_number ?? "");
  return (
    <div className="flex flex-wrap items-end gap-2 w-full">
      <Field label="Full name">
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Registration">
        <select
          className="input"
          value={regType}
          onChange={(e) => setRegType(e.target.value as StaffRegType)}
        >
          <option value="none">No registration</option>
          <option value="mara">MARA (MARN)</option>
          <option value="lpn">Legal practitioner (LPN)</option>
        </select>
      </Field>
      {regType !== "none" && (
        <Field label={regType === "mara" ? "MARN" : "LPN"}>
          <input className="input" value={number} onChange={(e) => setNumber(e.target.value)} />
        </Field>
      )}
      <button
        className="btn-primary"
        disabled={pending || !name.trim()}
        onClick={() =>
          onSave({
            full_name: name.trim(),
            registration_type: regType,
            registration_number: regType === "none" ? null : number.trim() || null,
            active: true,
          })
        }
      >
        Save
      </button>
      <button className="btn-secondary" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}
