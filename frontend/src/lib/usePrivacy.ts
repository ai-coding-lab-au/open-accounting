/**
 * Hooks that wrap the mask functions with the privacy toggle.
 *
 * When the toggle is off, hooks return the real value. When on, they return
 * the masked placeholder. Components stay clean: just swap `client.name` for
 * `useMaskName(client.name, "client")` at the display site.
 */

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

type NameKind = "client" | "company" | "provider";
type IdKind = "abn" | "acn" | "marn" | "bsb" | "account" | "swift";

export function usePrivacyEnabled(): boolean {
  return usePrivacyStore((s) => s.enabled);
}

export function useMaskName(real: string | null | undefined, kind: NameKind = "client"): string {
  const on = usePrivacyEnabled();
  if (!on) return real ?? "—";
  return maskName(real, kind);
}

export function useMaskMoney(real: string | number | null | undefined): string {
  const on = usePrivacyEnabled();
  if (!on) {
    if (real == null || real === "") return "—";
    const n = typeof real === "string" ? Number(real) : real;
    if (!Number.isFinite(n)) return String(real);
    return `$${n.toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  return maskMoney(real);
}

export function useMaskEmail(real: string | null | undefined): string {
  const on = usePrivacyEnabled();
  if (!on) return real ?? "—";
  return maskEmail(real);
}

export function useMaskPhone(real: string | null | undefined): string {
  const on = usePrivacyEnabled();
  if (!on) return real ?? "—";
  return maskPhone(real);
}

export function useMaskAddress(real: string | null | undefined): string {
  const on = usePrivacyEnabled();
  if (!on) return real ?? "—";
  return maskAddress(real);
}

export function useMaskId(real: string | null | undefined, kind: IdKind): string {
  const on = usePrivacyEnabled();
  if (!on) return real ?? "—";
  return maskId(real, kind);
}

export function useMaskDocNumber(real: string | null | undefined): string {
  const on = usePrivacyEnabled();
  if (!on) return real ?? "—";
  return maskDocNumber(real);
}
