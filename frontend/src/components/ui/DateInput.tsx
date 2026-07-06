import { useEffect, useRef, useState } from "react";

/**
 * Locale-independent date input that always *displays* DD/MM/YYYY (Australian
 * convention) while storing/emitting an ISO `YYYY-MM-DD` string.
 *
 * Native <input type="date"> renders the field in the browser's locale, so a
 * US-locale browser shows an AU date as MM/DD/YYYY — misleading. This wraps a
 * text field (the visible DD/MM/YYYY editor) next to a tiny native date picker
 * (the 📅 button) so users get both: an unambiguous display and a calendar.
 *
 * Controlled: `value` is ISO (or "") and `onChange` emits ISO (or "").
 *
 * Editing model: the visible text is the source of truth while the field is
 * focused, so typing is never clobbered by a re-sync. Every keystroke that
 * forms a complete valid DD/MM/YYYY emits ISO immediately; on blur the text is
 * reformatted from the committed ISO value.
 */
function isoToDisplay(iso: string): string {
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : "";
}

/** Parse a (possibly partial) DD/MM/YYYY string. Returns ISO or null. */
function displayToIso(text: string): string | null {
  const t = text.trim();
  if (!t) return "";
  const m = t.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) return null;
  const d = m[1].padStart(2, "0");
  const mo = m[2].padStart(2, "0");
  const y = m[3];
  const dn = Number(d);
  const mn = Number(mo);
  if (mn < 1 || mn > 12 || dn < 1 || dn > 31) return null;
  // Round-trip through Date to reject impossible dates like 31/02.
  const probe = new Date(`${y}-${mo}-${d}T00:00:00Z`);
  if (
    probe.getUTCFullYear() !== Number(y) ||
    probe.getUTCMonth() + 1 !== mn ||
    probe.getUTCDate() !== dn
  ) {
    return null;
  }
  return `${y}-${mo}-${d}`;
}

export function DateInput({
  value,
  onChange,
  className = "",
  id,
}: {
  value: string;
  onChange: (iso: string) => void;
  className?: string;
  id?: string;
}) {
  const [text, setText] = useState(() => isoToDisplay(value));
  const [invalid, setInvalid] = useState(false);
  const focusedRef = useRef(false);

  // Re-sync the visible text from the ISO value ONLY when the field isn't being
  // edited (e.g. form reset, AI fill, calendar pick). While focused, the text
  // the user is typing stays authoritative so keystrokes are never clobbered.
  useEffect(() => {
    if (!focusedRef.current) {
      setText(isoToDisplay(value));
      setInvalid(false);
    }
  }, [value]);

  const handleText = (raw: string) => {
    setText(raw);
    const iso = displayToIso(raw);
    if (iso === null) {
      // Incomplete or invalid: flag it AND clear the committed value, so the
      // parent never submits a stale/fallback date behind an invalid field
      // (BUG-A4). A non-empty invalid field therefore reads as "no date".
      setInvalid(true);
      onChange("");
    } else {
      setInvalid(false);
      onChange(iso); // "" when cleared, ISO when complete+valid
    }
  };

  const handleBlur = () => {
    focusedRef.current = false;
    // Snap the text back to the canonical display of whatever we committed.
    const iso = displayToIso(text);
    if (iso && iso !== "") {
      setText(isoToDisplay(iso));
      setInvalid(false);
    } else if (text.trim() === "") {
      setInvalid(false);
      onChange("");
    } else {
      // Leave the (invalid) text visible so the user can fix it.
      setInvalid(true);
    }
  };

  const syncFromPicker = (iso: string) => {
    focusedRef.current = false;
    onChange(iso);
    setText(isoToDisplay(iso));
    setInvalid(false);
  };

  return (
    <div className="relative flex items-center">
      <input
        id={id}
        type="text"
        inputMode="numeric"
        placeholder="DD/MM/YYYY"
        value={text}
        onFocus={() => (focusedRef.current = true)}
        onBlur={handleBlur}
        onChange={(e) => handleText(e.target.value)}
        className={`${className} ${invalid ? "border-rose-400" : ""}`}
        aria-invalid={invalid}
      />
      {/* A real native date input overlaid on the 📅 affordance: clicking it
          opens the OS calendar and its onChange fires normally (more reliable
          across browsers than a hidden input + showPicker()). It's visually
          transparent except for the emoji label behind it. */}
      <span className="absolute right-1 text-slate-400 text-sm pointer-events-none">
        📅
      </span>
      <input
        type="date"
        title="Pick a date"
        value={value}
        onChange={(e) => syncFromPicker(e.target.value)}
        className="absolute right-0 w-7 h-full opacity-0 cursor-pointer"
      />
    </div>
  );
}
