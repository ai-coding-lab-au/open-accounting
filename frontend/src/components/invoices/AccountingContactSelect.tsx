import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useCompanyStore } from "../../store/company";
import type { Contact, ContactCreate, InvoiceDirection } from "../../types/api";

interface Props {
  direction: InvoiceDirection;
  contactId: number | null;
  contactName: string;
  contactAbn: string;
  onChange: (contact: Contact | null) => void;
}

const EMPTY_CONTACT: ContactCreate = {
  name: "",
  kind: "supplier",
  abn: "",
  email: "",
  phone: "",
  address: "",
  notes: "",
};

export default function AccountingContactSelect({
  direction,
  contactId,
  contactName,
  contactAbn,
  onChange,
}: Props) {
  const currentId = useCompanyStore((state) => state.currentId);
  const currentGeneration = useCompanyStore((state) => state.currentGeneration);
  const companyIdentity = `${currentId ?? ""}:${currentGeneration ?? ""}`;
  const [companyAtSelection, setCompanyAtSelection] = useState(companyIdentity);
  const [selectedContact, setSelectedContact] = useState<Contact | null>(null);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [isOpen, setIsOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [newContact, setNewContact] = useState<ContactCreate>({
    ...EMPTY_CONTACT,
    kind: direction === "AP" ? "supplier" : "customer",
  });
  const queryClient = useQueryClient();
  const label = direction === "AP" ? "Supplier" : "Customer";
  const compatibleKinds = direction === "AP" ? ["supplier", "both"] : ["customer", "both"];
  const identityMatches = companyAtSelection === companyIdentity;

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(timeout);
  }, [search]);

  useEffect(() => {
    if (companyAtSelection === companyIdentity) return;
    setCompanyAtSelection(companyIdentity);
    setSelectedContact(null);
    setSearch("");
    setDebouncedSearch("");
    setIsOpen(false);
    onChange(null);
  }, [companyAtSelection, companyIdentity, onChange]);

  useEffect(() => {
    if (contactId === null) setSelectedContact(null);
  }, [contactId]);

  const contactQuery = useQuery({
    queryKey: ["contacts", currentId, currentGeneration, "invoice-picker", debouncedSearch],
    enabled: !!currentId && !!currentGeneration && identityMatches,
    queryFn: async () => {
      const headers = {
        "X-Company-Id": currentId!,
        "X-Company-Generation": currentGeneration!,
      };
      const response = await api.get<Contact[]>("/contacts", {
        params: { active_only: true, ...(debouncedSearch ? { q: debouncedSearch } : {}) },
        headers,
      });
      return response.data;
    },
  });

  const creation = useMutation({
    mutationFn: async (payload: ContactCreate) => {
      const headers = {
        "X-Company-Id": currentId!,
        "X-Company-Generation": currentGeneration!,
      };
      return (await api.post<Contact>("/contacts", payload, { headers })).data;
    },
    onSuccess: async (contact) => {
      const currentCompany = useCompanyStore.getState();
      if (
        currentCompany.currentId !== currentId ||
        currentCompany.currentGeneration !== currentGeneration
      ) return;
      await queryClient.invalidateQueries({
        queryKey: ["contacts", currentId, currentGeneration],
      });
      const latestCompany = useCompanyStore.getState();
      if (
        latestCompany.currentId !== currentId ||
        latestCompany.currentGeneration !== currentGeneration
      ) return;
      setSelectedContact(contact);
      onChange(contact);
      setIsCreating(false);
      setIsOpen(false);
      setSearch("");
      setNewContact({ ...EMPTY_CONTACT, kind: direction === "AP" ? "supplier" : "customer" });
    },
  });

  const choices = (search.trim() === debouncedSearch ? contactQuery.data ?? [] : [])
    .filter((contact) => contact.active && compatibleKinds.includes(contact.kind))
    .slice(0, 8);
  const selected = identityMatches && selectedContact?.id === contactId
    ? selectedContact
    : null;

  const selectContact = (contact: Contact) => {
    if (!contact.active || !compatibleKinds.includes(contact.kind)) return;
    setSelectedContact(contact);
    onChange(contact);
    setIsOpen(false);
  };

  const setNewField = <K extends keyof ContactCreate>(key: K, value: ContactCreate[K]) => {
    setNewContact((previous) => ({ ...previous, [key]: value }));
  };

  const submitNewContact = () => {
    creation.mutate({
      ...newContact,
      name: newContact.name.trim(),
      kind: direction === "AP" ? "supplier" : "customer",
    });
  };

  return (
    <div className="space-y-2">
      <label className="block text-sm">
        <span className="block text-slate-600 mb-1">{label}</span>
        <input
          className="input"
          aria-label={`Search ${label.toLowerCase()} contacts`}
          aria-required="true"
          placeholder={`Search ${label.toLowerCase()} by name`}
          value={search}
          onFocus={() => setIsOpen(true)}
          onChange={(event) => {
            setSearch(event.target.value);
            setIsOpen(true);
          }}
        />
      </label>

      {selected && identityMatches && (
        <div className="rounded border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm" aria-label="Selected accounting contact">
          <div className="flex items-start justify-between gap-3">
            <div>
              <strong>{selected.name}</strong>
              <span className="ml-2 text-slate-600">{selected.kind}</span>
              <div className="text-slate-600">
                {[selected.abn && `ABN ${selected.abn}`, selected.email, selected.phone]
                  .filter(Boolean).join(" · ") || "No additional contact details"}
              </div>
            </div>
            <button type="button" className="btn-secondary shrink-0" onClick={() => onChange(null)}>
              Clear
            </button>
          </div>
        </div>
      )}

      {contactId === null && (
        <p role="status" className="text-xs text-slate-600">
          {direction === "AP" ? "Select a supplier." : "Select a customer."}
        </p>
      )}

      {isOpen && identityMatches && (
        <div className="max-h-56 overflow-auto rounded border border-slate-200 bg-white" role="listbox" aria-label={`${label} contact results`}>
          {contactQuery.isError ? (
            <p className="px-3 py-2 text-sm text-red-700">{apiErrorMessage(contactQuery.error)}</p>
          ) : choices.length ? choices.map((contact) => (
            <button
              type="button"
              role="option"
              aria-selected={contact.id === contactId}
              aria-label={`Select ${contact.name}`}
              key={contact.id}
              className="block w-full border-b border-slate-100 px-3 py-2 text-left text-sm last:border-b-0 hover:bg-slate-50"
              onClick={() => selectContact(contact)}
            >
              <span className="font-medium">{contact.name}</span>
              <span className="ml-2 text-slate-500">{contact.kind}</span>
              <span className="mt-0.5 block text-xs text-slate-600">
                {[contact.abn && `ABN ${contact.abn}`, contact.email, contact.phone]
                  .filter(Boolean).join(" · ") || "No additional contact details"}
              </span>
            </button>
          )) : (
            <p className="px-3 py-2 text-sm text-slate-500">
              {contactQuery.isLoading ? "Loading contacts…" : "No compatible active contacts found."}
            </p>
          )}
        </div>
      )}

      <button
        type="button"
        className="text-sm font-medium text-emerald-800 hover:underline"
        onClick={() => {
          setNewContact({ ...EMPTY_CONTACT, kind: direction === "AP" ? "supplier" : "customer" });
          setIsCreating(true);
        }}
      >
        + New {label.toLowerCase()}
      </button>

      {isCreating && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4" role="presentation">
          <div className="w-full max-w-lg rounded-lg bg-surface p-5 shadow-xl" role="dialog" aria-modal="true" aria-labelledby="new-accounting-contact-title">
            <h3 id="new-accounting-contact-title" className="mb-4 text-lg font-semibold">
              New {label.toLowerCase()}
            </h3>
            <div className="grid grid-cols-2 gap-3">
              <ContactField label="Name" value={newContact.name} onChange={(value) => setNewField("name", value)} />
              <ContactField label="ABN" value={newContact.abn ?? ""} onChange={(value) => setNewField("abn", value)} />
              <ContactField label="Email" value={newContact.email ?? ""} onChange={(value) => setNewField("email", value)} />
              <ContactField label="Phone" value={newContact.phone ?? ""} onChange={(value) => setNewField("phone", value)} />
              <ContactField label="Address" value={newContact.address ?? ""} onChange={(value) => setNewField("address", value)} />
              <ContactField label="Notes" value={newContact.notes ?? ""} onChange={(value) => setNewField("notes", value)} />
            </div>
            {creation.isError && (
              <p role="alert" className="mt-3 text-sm text-red-700">{apiErrorMessage(creation.error)}</p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button type="button" className="btn-secondary" onClick={() => setIsCreating(false)} disabled={creation.isPending}>Cancel</button>
              <button type="button" className="btn-primary" onClick={submitNewContact} disabled={creation.isPending || !newContact.name.trim()}>
                {creation.isPending ? "Creating…" : `Create ${label.toLowerCase()}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {!selected && contactId !== null && identityMatches && (
        <p className="text-sm text-slate-700">Selected: {contactName}{contactAbn ? ` · ABN ${contactAbn}` : ""}</p>
      )}
    </div>
  );
}

function ContactField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-slate-600">{label}</span>
      <input className="input" aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
