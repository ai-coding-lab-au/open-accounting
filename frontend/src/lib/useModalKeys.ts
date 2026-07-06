import { useEffect, useRef } from "react";

const modalStack: symbol[] = [];

export function useModalKeys({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit?: () => void;
}) {
  const idRef = useRef(Symbol("modal"));
  const onCloseRef = useRef(onClose);
  const onSubmitRef = useRef(onSubmit);

  useEffect(() => {
    onCloseRef.current = onClose;
    onSubmitRef.current = onSubmit;
  }, [onClose, onSubmit]);

  useEffect(() => {
    if (!open) return;

    const id = idRef.current;
    modalStack.push(id);

    const handler = (e: KeyboardEvent) => {
      if (modalStack[modalStack.length - 1] !== id) return;

      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCloseRef.current();
        return;
      }

      if (e.key === "Enter" && onSubmitRef.current) {
        const target = e.target as HTMLElement | null;
        const tag = target?.tagName?.toLowerCase();
        if (tag === "textarea" || tag === "button" || tag === "select") return;
        if (target?.isContentEditable) return;
        e.preventDefault();
        e.stopPropagation();
        onSubmitRef.current();
      }
    };

    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      const idx = modalStack.lastIndexOf(id);
      if (idx >= 0) modalStack.splice(idx, 1);
    };
  }, [open]);
}
