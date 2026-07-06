import { create } from "zustand";
import { persist } from "zustand/middleware";
import { DEFAULT_THEME, isThemeId, type ThemeId } from "../lib/themes";

interface ThemeStore {
  theme: ThemeId;
  setTheme: (id: ThemeId) => void;
}

function applyToDOM(id: ThemeId) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", id);
  }
}

export const useThemeStore = create<ThemeStore>()(
  persist(
    (set) => ({
      theme: DEFAULT_THEME,
      setTheme: (id) => {
        applyToDOM(id);
        set({ theme: id });
      },
    }),
    {
      name: "accounting.theme",
      onRehydrateStorage: () => (state) => {
        if (state && isThemeId(state.theme)) {
          applyToDOM(state.theme);
        } else {
          applyToDOM(DEFAULT_THEME);
        }
      },
    },
  ),
);
