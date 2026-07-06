/**
 * Minimal non-blocking toast — a drop-in replacement for window.alert() in
 * fire-and-forget error paths (e.g. a failed PDF download). Unlike alert(),
 * it doesn't block the JS main thread, so it works in automation / WebView
 * contexts where alert() silently freezes the page.
 *
 * Deliberately dependency-free and DOM-direct: it's only used for transient,
 * non-critical notices. For anything the user must act on, use a real modal.
 */
export function toast(message: string, kind: "error" | "info" = "info"): void {
  if (typeof document === "undefined") return;
  const el = document.createElement("div");
  el.textContent = message;
  el.setAttribute("role", "status");
  const base =
    "position:fixed;z-index:9999;bottom:1rem;left:50%;transform:translateX(-50%);" +
    "max-width:90vw;padding:0.6rem 1rem;border-radius:0.5rem;font-size:0.875rem;" +
    "box-shadow:0 4px 16px rgba(0,0,0,0.18);transition:opacity .25s;opacity:0;";
  const palette =
    kind === "error"
      ? "background:#e11d48;color:#fff;"
      : "background:#0f172a;color:#fff;";
  el.setAttribute("style", base + palette);
  document.body.appendChild(el);
  // next frame → fade in
  requestAnimationFrame(() => {
    el.style.opacity = "1";
  });
  const ttl = kind === "error" ? 6000 : 3000;
  window.setTimeout(() => {
    el.style.opacity = "0";
    window.setTimeout(() => el.remove(), 300);
  }, ttl);
}
