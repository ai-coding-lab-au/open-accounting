import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Company } from "../types/api";

type CompanyIdentity = Pick<Company, "id" | "generation_id">;

interface PersistedCompanyState {
  currentId: string | null;
  currentGeneration: string | null;
}

interface CompanyStore extends PersistedCompanyState {
  setCurrent: (company: CompanyIdentity | null) => void;
}

function migratePersistedCompanyState(state: unknown): PersistedCompanyState {
  const persisted = state as {
    currentId?: unknown;
    currentGeneration?: unknown;
  } | null;
  const currentId =
    typeof persisted?.currentId === "string" ? persisted.currentId : null;
  const currentGeneration =
    currentId && typeof persisted?.currentGeneration === "string"
      ? persisted.currentGeneration
      : null;

  return { currentId, currentGeneration };
}

export const useCompanyStore = create<CompanyStore>()(
  persist<CompanyStore, [], [], PersistedCompanyState>(
    (set) => ({
      currentId: null,
      currentGeneration: null,
      setCurrent: (company) =>
        set({
          currentId: company?.id ?? null,
          currentGeneration: company?.generation_id ?? null,
        }),
    }),
    {
      name: "accounting.currentCompany",
      version: 1,
      partialize: ({ currentId, currentGeneration }) => ({
        currentId,
        currentGeneration,
      }),
      // Version 0 persisted only currentId. Preserve that selection and let the
      // company-list response fill its generation before the workspace remounts.
      migrate: migratePersistedCompanyState,
    }
  )
);
