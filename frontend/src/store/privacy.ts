import { create } from "zustand";
import { persist } from "zustand/middleware";

interface PrivacyStore {
  enabled: boolean;
  toggle: () => void;
  set: (v: boolean) => void;
}

export const usePrivacyStore = create<PrivacyStore>()(
  persist(
    (set, get) => ({
      enabled: false,
      toggle: () => set({ enabled: !get().enabled }),
      set: (v) => set({ enabled: v }),
    }),
    { name: "accounting.privacy" }
  )
);
