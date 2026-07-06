/**
 * Privacy mask: deterministic, in-memory mapping from real strings to opaque
 * placeholders. Used when the user enables the privacy toggle to share
 * screenshots without leaking PII.
 *
 * Rules:
 *  - Names map to "Client A / B / C ...", "Company A ...", "Provider A ..."
 *    (stable for the lifetime of the tab — same real name → same letter).
 *  - Money is always "$X,XXX.XX" regardless of value.
 *  - Contacts (email/phone/address) map to fixed placeholders.
 *  - IDs (ABN/ACN/MARN/BSB/account) map to format-preserving X strings.
 *
 * Maps are per-tab (a Map in module state). Refreshing the page resets them,
 * which is fine: the goal is "what's on the screen right now", not stable
 * pseudonyms across sessions.
 */

type NameKind = "client" | "company" | "provider";

const NAME_LABEL: Record<NameKind, string> = {
  client: "Client",
  company: "Company",
  provider: "Provider",
};

const nameMaps: Record<NameKind, Map<string, string>> = {
  client: new Map(),
  company: new Map(),
  provider: new Map(),
};

function letterFor(n: number): string {
  // 0 -> A, 25 -> Z, 26 -> AA, 27 -> AB ...
  let s = "";
  let x = n;
  while (true) {
    s = String.fromCharCode(65 + (x % 26)) + s;
    if (x < 26) return s;
    x = Math.floor(x / 26) - 1;
  }
}

export function maskName(real: string | null | undefined, kind: NameKind = "client"): string {
  if (!real) return "—";
  const map = nameMaps[kind];
  const hit = map.get(real);
  if (hit) return hit;
  const label = `${NAME_LABEL[kind]} ${letterFor(map.size)}`;
  map.set(real, label);
  return label;
}

export function maskMoney(_v: unknown): string {
  return "$X,XXX.XX";
}

export function maskEmail(_v: string | null | undefined): string {
  return "user@example.com";
}

export function maskPhone(_v: string | null | undefined): string {
  return "+61 4XX XXX XXX";
}

export function maskAddress(_v: string | null | undefined): string {
  return "—";
}

type IdKind = "abn" | "acn" | "marn" | "bsb" | "account" | "swift";

export function maskId(_v: string | null | undefined, kind: IdKind): string {
  switch (kind) {
    case "abn":
      return "XX XXX XXX XXX";
    case "acn":
      return "XXX XXX XXX";
    case "marn":
      return "XXXXXXX";
    case "bsb":
      return "XXX-XXX";
    case "account":
      return "XXXX XXXX";
    case "swift":
      return "XXXXXXXX";
  }
}

export function maskDocNumber(real: string | null | undefined): string {
  // Doc numbers like SA-2026-0001 / INV-001 — preserve the prefix shape but
  // replace the trailing digits with X so the user can still see "this is an
  // SA" vs "this is an INV" while not exposing real numbering.
  if (!real) return "—";
  return real.replace(/\d/g, "X");
}
