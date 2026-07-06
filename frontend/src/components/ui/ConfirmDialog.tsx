import { useModalKeys } from "../../lib/useModalKeys";

/**
 * In-app confirmation dialog, replacing the native window.confirm().
 *
 * window.confirm() blocks the JS main thread and is unavailable in
 * automation / WebView / some extension contexts, where it silently
 * freezes the page. This is a normal React modal: non-blocking, themeable,
 * and keyboard-operable (Esc cancels, Enter confirms).
 *
 * Controlled by `open`; render it unconditionally and toggle `open`.
 */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useModalKeys({ open, onClose: onCancel, onSubmit: onConfirm });
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60] bg-black/40 flex items-center justify-center p-4">
      <div className="bg-surface rounded-lg shadow-xl border border-slate-200 w-full max-w-md p-5 space-y-4">
        <h2 className="font-semibold text-base">{title}</h2>
        {message && <div className="text-sm text-slate-600">{message}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            className="btn-secondary"
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={destructive ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
