import { create } from "zustand";
import { persist } from "zustand/middleware";

interface CompanyStore {
  currentId: string | null;
  setCurrent: (id: string | null) => void;
}

export const useCompanyStore = create<CompanyStore>()(
  persist(
    (set) => ({
      currentId: null,
      setCurrent: (id) => set({ currentId: id }),
    }),
    { name: "accounting.currentCompany" }
  )
);
