import { useEffect, useRef, useState } from "react";

const MONTHS: Record<string, number> = {
  jan: 1, january: 1,
  feb: 2, february: 2,
  mar: 3, march: 3,
  apr: 4, april: 4,
  may: 5,
  jun: 6, june: 6,
  jul: 7, july: 7,
  aug: 8, august: 8,
  sep: 9, sept: 9, september: 9,
  oct: 10, october: 10,
  nov: 11, november: 11,
  dec: 12, december: 12,
};

function toIso(year: number, month: number, day: number): string | null {
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const d = new Date(year, month - 1, day);
  if (d.getFullYear() !== year || d.getMonth() !== month - 1 || d.getDate() !== day) {
    return null;
  }
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

// Parse the value we hold/receive into ISO (YYYY-MM-DD), tolerating the free
// formats that field extraction and legacy notes produce: ISO, DD/MM/YYYY, and
// named-month forms like "January 1 1986", "12 March 1990", "1 Jan 1986".
// Returns null if it can't be confidently parsed.
function anyToIso(value: string): string | null {
  const s = (value || "").trim();
  if (!s) return null;

  const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s);
  if (iso) return toIso(Number(iso[1]), Number(iso[2]), Number(iso[3]));

  const dmy = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s);
  if (dmy) return toIso(Number(dmy[3]), Number(dmy[2]), Number(dmy[1]));

  // Named month, either "1 January 1986" / "1 Jan 1986" or "January 1 1986".
  const lower = s.toLowerCase().replace(/,/g, " ").replace(/\s+/g, " ").trim();
  let m = /^(\d{1,2})\s+([a-z]+)\s+(\d{4})$/.exec(lower);
  if (m && MONTHS[m[2]]) return toIso(Number(m[3]), MONTHS[m[2]], Number(m[1]));
  m = /^([a-z]+)\s+(\d{1,2})\s+(\d{4})$/.exec(lower);
  if (m && MONTHS[m[1]]) return toIso(Number(m[3]), MONTHS[m[1]], Number(m[2]));

  return null;
}

function isoToDisplay(value: string): string {
  const iso = anyToIso(value);
  if (iso) {
    const [y, mo, d] = iso.split("-");
    return `${d}/${mo}/${y}`;
  }
  // Unparseable — show whatever we hold so nothing is silently lost.
  return value || "";
}

// Auto-mask DD/MM/YYYY as the operator types. Two ways to advance a segment:
//   - type the slash yourself ("1/6/2026") — we keep it, and
//   - let it auto-advance when a segment fills to 2 digits ("01" -> "01/").
// We honour the slashes the user typed (so a 1-digit day/month + "/" works) and
// only *add* a boundary slash, never remove one. Free-text with letters (e.g. a
// pasted "January 1986") is left alone so it can be parsed on blur.
function maskTyping(raw: string): string {
  if (/[a-z]/i.test(raw)) return raw;
  const trailingSlash = raw.endsWith("/");
  const caps = [2, 2, 4];
  const segs = raw
    .split("/")
    .slice(0, 3)
    .map((s, i) => s.replace(/\D/g, "").slice(0, caps[i]));
  // Drop empty trailing segments the split may have produced, but remember if
  // the user just typed a slash so we preserve that boundary below.
  while (segs.length > 1 && segs[segs.length - 1] === "") segs.pop();

  let out = segs.join("/");
  const last = segs[segs.length - 1] ?? "";
  // Auto-advance: day/month filled to 2 digits → add the next "/" for them.
  if (segs.length < 3 && last.length === caps[segs.length - 1]) out += "/";
  // Preserve a slash the user typed (1-digit segment + "/").
  else if (trailingSlash && segs.length < 3 && last !== "") out += "/";
  return out;
}

function displayToIso(value: string): string | null {
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(value.trim());
  if (!m) return anyToIso(value); // tolerate free-text on commit too
  return toIso(Number(m[3]), Number(m[2]), Number(m[1]));
}

export default function DateInput({
  value,
  onChange,
  className = "input",
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  className?: string;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState(() => isoToDisplay(value));
  const [invalid, setInvalid] = useState(false);
  const focusedRef = useRef(false);
  const prevLenRef = useRef(draft.length);

  // Re-sync the visible text from the stored value ONLY when not being edited,
  // so a parent re-render where `value` hasn't changed can't reset what the
  // user is typing back to the old display (the J-DATE-1 regression class).
  // Also skip while a bad date is on screen so we don't silently wipe it.
  useEffect(() => {
    if (!focusedRef.current && !invalid) setDraft(isoToDisplay(value));
  }, [value, invalid]);

  // Normalise a free-text / legacy value (e.g. extracted "January 1 1986") to
  // stored ISO once, so the form persists a canonical date even if the operator
  // never edits the field. Only fires when the incoming value parses to a
  // *different* string than what's stored, and never while being edited.
  useEffect(() => {
    if (focusedRef.current) return;
    const iso = anyToIso(value);
    if (iso && iso !== value) onChange(iso);
  }, [value, onChange]);

  return (
    <>
      <input
        type="text"
        className={`${className} ${invalid ? "ring-1 ring-rose-400 border-rose-400" : ""}`}
        inputMode="numeric"
        autoComplete="off"
        placeholder="DD/MM/YYYY"
        value={draft}
        disabled={disabled}
        aria-invalid={invalid}
        onFocus={() => (focusedRef.current = true)}
        onChange={(e) => {
          const incoming = e.target.value;
          const growing = incoming.length > prevLenRef.current;
          // Only auto-insert slashes while adding characters, never while deleting
          // (so backspace can remove a slash instead of it snapping back).
          const next = growing ? maskTyping(incoming) : incoming;
          prevLenRef.current = next.length;
          setDraft(next);
          if (next.trim() === "") {
            setInvalid(false);
            onChange("");
            return;
          }
          const iso = displayToIso(next);
          // Emit ISO when complete+valid; clear (emit "") when invalid so the
          // form can't submit a stale/fallback date behind an invalid field.
          onChange(iso ?? "");
          if (iso) setInvalid(false);
        }}
        onBlur={() => {
          focusedRef.current = false;
          const raw = draft.trim();
          if (raw === "") {
            setInvalid(false);
            prevLenRef.current = 0;
            setDraft("");
            return;
          }
          const iso = anyToIso(raw);
          if (iso) {
            setInvalid(false);
            const committed = isoToDisplay(iso);
            prevLenRef.current = committed.length;
            setDraft(committed);
          } else {
            // Keep the bad text visible + flag it instead of silently wiping it,
            // so the operator can see why the field won't accept their entry.
            setInvalid(true);
            prevLenRef.current = raw.length;
          }
        }}
      />
      {invalid && (
        <span className="block text-xs text-rose-600 mt-0.5">
          That date doesn't exist — use DD/MM/YYYY.
        </span>
      )}
    </>
  );
}
