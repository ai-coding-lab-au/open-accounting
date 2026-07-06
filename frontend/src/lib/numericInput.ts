import type { KeyboardEvent } from "react";

/**
 * onKeyDown guard for money/quantity `<input type="number">` fields.
 *
 * Native number inputs accept scientific notation ("1e5") and a leading "+",
 * which silently expand to surprising values (1e5 → 100000) and are never
 * valid money entry. This blocks the keystrokes that introduce them while
 * leaving digits, ".", "-" (caller can still set min=0), navigation and
 * editing keys untouched.
 *
 * Usage: <input type="number" onKeyDown={blockScientificNotation} ... />
 */
export function blockScientificNotation(e: KeyboardEvent<HTMLInputElement>): void {
  if (e.key === "e" || e.key === "E" || e.key === "+") {
    e.preventDefault();
  }
}

/**
 * Normalise a money/quantity string typed into a `type="text"` field.
 *
 * Native `<input type="number">` handles "1,000" inconsistently across
 * browsers (the comma can be dropped so "1,000" becomes "1000" — or worse,
 * mis-concatenated). For predictable, cross-browser behaviour use a text
 * input and clean the value on change: strip thousands-separator commas and
 * any character that isn't a digit or a decimal point, and collapse multiple
 * decimal points to the first one. Returns "" for empty/invalid input so the
 * caller's Number()/parseFloat sees a clean string.
 */
export function cleanMoney(value: string): string {
  const stripped = value.replace(/[^\d.]/g, "");
  const firstDot = stripped.indexOf(".");
  if (firstDot === -1) return stripped;
  return (
    stripped.slice(0, firstDot + 1) +
    stripped.slice(firstDot + 1).replace(/\./g, "")
  );
}

/**
 * Add thousands-separator commas to a cleaned money string for DISPLAY in an
 * editable field. Only the integer part is grouped; the decimal part (and a
 * trailing ".") is left exactly as the user typed it, so "1234.5" → "1,234.5"
 * and "1234." → "1,234." (a half-typed decimal isn't disturbed). Input must
 * already be cleanMoney-cleaned (digits + at most one dot).
 */
export function groupThousands(cleaned: string): string {
  if (cleaned === "") return "";
  const dot = cleaned.indexOf(".");
  const intPart = dot === -1 ? cleaned : cleaned.slice(0, dot);
  const rest = dot === -1 ? "" : cleaned.slice(dot); // includes the "."
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return grouped + rest;
}
