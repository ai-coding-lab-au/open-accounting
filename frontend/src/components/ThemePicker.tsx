import { useMemo, useState } from "react";
import { useThemeStore } from "../store/theme";
import { THEMES, type ThemeMeta } from "../lib/themes";

// Quick top-bar dropdown — small, just lists every theme grouped by family.
export function ThemePickerCompact() {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const grouped = useMemo(() => groupByFamily(THEMES), []);

  return (
    <select
      value={theme}
      onChange={(e) => setTheme(e.target.value as ThemeMeta["id"])}
      className="text-xs px-2 py-1 rounded border border-slate-300 bg-surface text-slate-700 hover:bg-slate-100"
      title="Switch theme"
    >
      {Object.entries(grouped).map(([family, list]) => (
        <optgroup key={family} label={family}>
          {list.map((t) => (
            <option key={t.id} value={t.id}>
              {t.label}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

// Full grid chooser — for the Settings page. Shows a swatch preview per theme.
export function ThemePickerGrid() {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const [filter, setFilter] = useState<string>("All");
  const families = ["All", ...new Set(THEMES.map((t) => t.family))];

  const visible = filter === "All" ? THEMES : THEMES.filter((t) => t.family === filter);

  return (
    <div>
      <div className="flex gap-2 mb-3 text-xs">
        {families.map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            className={
              "px-2 py-1 rounded border " +
              (filter === f
                ? "bg-emerald-600 text-white border-emerald-700"
                : "bg-surface border-slate-300 text-slate-600 hover:bg-slate-100")
            }
          >
            {f}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {visible.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTheme(t.id)}
            className={
              "text-left rounded border overflow-hidden transition " +
              (theme === t.id
                ? "border-emerald-500 ring-2 ring-emerald-400"
                : "border-slate-300 hover:border-slate-400")
            }
          >
            <Swatch themeId={t.id} />
            <div className="px-3 py-2 bg-surface">
              <div className="text-sm font-medium text-slate-800">{t.label}</div>
              <div className="text-[11px] text-slate-500 mt-0.5 leading-snug">
                {t.description}
              </div>
              <div className="text-[10px] uppercase tracking-wider text-slate-400 mt-1">
                {t.family}
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

// Tiny isolated preview that renders a mini UI fragment using the target
// theme's CSS vars. Using nested [data-theme] to scope the vars.
function Swatch({ themeId }: { themeId: string }) {
  return (
    <div data-theme={themeId} style={{ backgroundColor: "var(--color-page)" }}>
      <div className="flex h-16">
        <div className="w-1/3 bg-slate-900 flex items-center justify-center">
          <span className="text-[10px] text-slate-100">Sidebar</span>
        </div>
        <div className="flex-1 bg-surface relative">
          <div className="absolute top-1 left-1 right-1 h-2 bg-slate-200 rounded" />
          <div className="absolute top-5 left-1 w-1/2 h-2 bg-slate-100 rounded" />
          <div className="absolute top-9 left-1 w-1/3 h-2 bg-slate-100 rounded" />
          <div className="absolute bottom-1 right-1 h-3 w-8 bg-emerald-600 rounded" />
        </div>
      </div>
    </div>
  );
}

function groupByFamily(themes: ThemeMeta[]): Record<string, ThemeMeta[]> {
  const out: Record<string, ThemeMeta[]> = {};
  for (const t of themes) {
    if (!out[t.family]) out[t.family] = [];
    out[t.family].push(t);
  }
  return out;
}
