import { usePrivacyStore } from "../store/privacy";
import {
  maskAddress,
  maskDocNumber,
  maskEmail,
  maskId,
  maskMoney,
  maskName,
  maskPhone,
} from "./mask";

/**
 * All format helpers read the privacy toggle directly from the Zustand store
 * (not via a hook) so they can be called from anywhere — including inside
 * `.map(...)` callbacks where hooks are illegal.
 *
 * Components don't need to know whether privacy is on; they just call
 * `formatMoney(value)` / `displayName(client.name, "client")` etc., and the
 * functions decide.
 *
 * The toggle is a reactive store value, but these helpers don't subscribe to
 * it — re-rendering is driven by the TopBar button (which DOES subscribe via
 * the hook). When the user flips the toggle, TopBar re-renders, which causes
 * its parent layout subtree (the whole app) to re-render via prop/state
 * changes; in practice React Query also re-runs selectors on state changes,
 * so the masked output appears immediately.
 *
 * If you ever see "toggle clicked but UI didn't update", make the calling
 * component subscribe to the store via usePrivacyEnabled() so it knows to
 * re-render.
 */

function privacyOn(): boolean {
  return usePrivacyStore.getState().enabled;
}

export function formatMoney(value: string | number | null | undefined, currency = "AUD"): string {
  if (privacyOn()) return maskMoney(value);
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(n)) return String(value);
  try {
    return new Intl.NumberFormat("en-AU", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(n);
  } catch {
    // `currency` isn't a valid ISO 4217 code yet — e.g. mid-keystroke in an
    // editable currency field ("A", "AU"). Fall back to a plain number so a
    // partial edit never crashes the render.
    return n.toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
}

// Summarise Service Agreement service items for a list's "Visa items" column.
// Visa applications show "Subclass NNN"; reply_s56 shows a fixed compact label;
// other items fall back to their description, else a humanised item_type. Used
// by both the SA list and the Outgoing list so they read identically.
export function summariseVisaItems(
  items: { item_type?: string | null; visa_subclass?: string | null; description?: string | null }[] | null | undefined,
): string {
  const labels = (items ?? [])
    .map((item) => {
      const subclass = (item.visa_subclass ?? "").trim();
      const itemType = (item.item_type ?? "visa_application").trim();
      if (itemType === "visa_application" && subclass) return `Subclass ${subclass}`;
      if (itemType === "reply_s56_request") return "Reply to s56";
      const description = (item.description ?? "").trim();
      if (description) return description;
      return itemType.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    })
    .filter(Boolean);
  // Dedup while preserving order.
  const seen = new Set<string>();
  const unique = labels.filter((l) => (seen.has(l) ? false : (seen.add(l), true)));
  return unique.length ? unique.join(", ") : "—";
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const s = value.slice(0, 10);
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return value;
  return m[3] + "/" + m[2] + "/" + m[1];
}

type NameKind = "client" | "company" | "provider";
type IdKind = "abn" | "acn" | "marn" | "bsb" | "account" | "swift";

export function displayName(real: string | null | undefined, kind: NameKind = "client"): string {
  if (privacyOn()) return maskName(real, kind);
  return real ?? "—";
}

export function displayEmail(real: string | null | undefined): string {
  if (privacyOn()) return maskEmail(real);
  return real ?? "—";
}

export function displayPhone(real: string | null | undefined): string {
  if (privacyOn()) return maskPhone(real);
  return real ?? "—";
}

export function displayAddress(real: string | null | undefined): string {
  if (privacyOn()) return maskAddress(real);
  return real ?? "—";
}

export function displayId(real: string | null | undefined, kind: IdKind): string {
  if (privacyOn()) return maskId(real, kind);
  return real ?? "—";
}

export function displayDocNumber(real: string | null | undefined): string {
  if (privacyOn()) return maskDocNumber(real);
  return real ?? "—";
}

// Human label for a document status (turns "partially_paid" into "Partially
// paid", etc.). Falls back to a title-cased version of unknown statuses.
export function statusLabel(status: string): string {
  switch (status) {
    case "partially_paid":
      return "Partially paid";
    default:
      return status.charAt(0).toUpperCase() + status.slice(1);
  }
}

export function statusBadgeClass(status: string): string {
  switch (status) {
    case "paid":
      return "bg-emerald-100 text-emerald-800 border border-emerald-200";
    case "partial":
    case "partially_paid":
      return "bg-amber-100 text-amber-800 border border-amber-200";
    case "void":
      return "bg-rose-100 text-rose-800 border border-rose-200 line-through";
    case "unpaid":
    default:
      return "bg-slate-100 text-slate-700 border border-slate-200";
  }
}
