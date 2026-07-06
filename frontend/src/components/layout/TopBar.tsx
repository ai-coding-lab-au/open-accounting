import CompanySwitcher from "./CompanySwitcher";
import { usePrivacyStore } from "../../store/privacy";
import { ThemePickerCompact } from "../ThemePicker";

export default function TopBar() {
  const enabled = usePrivacyStore((s) => s.enabled);
  const toggle = usePrivacyStore((s) => s.toggle);

  return (
    <header className="h-14 bg-surface border-b border-slate-200 flex items-center justify-between gap-4 px-4">
      <div className="text-sm text-slate-500 whitespace-nowrap truncate hidden md:block">
        Local-first · No data leaves this machine
      </div>
      <div className="flex items-center gap-3 shrink-0">
        <ThemePickerCompact />
        <button
          type="button"
          onClick={toggle}
          title={
            enabled
              ? "Privacy mode ON — names, contacts, amounts and IDs are masked. Click to turn off."
              : "Privacy mode OFF — click to mask PII for safe screenshots."
          }
          className={
            "text-xs px-2 py-1 rounded border transition " +
            (enabled
              ? "bg-amber-100 border-amber-300 text-amber-900 hover:bg-amber-200"
              : "bg-surface border-slate-300 text-slate-600 hover:bg-slate-100")
          }
        >
          {enabled ? "🔒 Privacy ON" : "🔓 Privacy OFF"}
        </button>
        <CompanySwitcher />
      </div>
    </header>
  );
}
