/**
 * Date helpers that keep us in the user's *local* timezone.
 *
 * `new Date().toISOString().slice(0, 10)` returns the UTC date, which is the
 * previous day in Sydney mornings (UTC+10/11). The whole app is single-user
 * single-timezone, so we always want "today as the user sees it".
 */

export function todayLocal(): string {
  return toLocalIsoDate(new Date());
}

export function toLocalIsoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function addDaysIso(isoDate: string, days: number): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  if (!year || !month || !day) return isoDate;
  const d = new Date(year, month - 1, day);
  d.setDate(d.getDate() + days);
  return toLocalIsoDate(d);
}
