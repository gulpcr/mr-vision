"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useFocusTrap } from "./useFocusTrap";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: "sm" | "md" | "lg";
}

const SIZE_CLASS: Record<NonNullable<ModalProps["size"]>, string> = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-2xl",
};

// The base every custom overlay in the app must use: role="dialog", aria-modal,
// focus trap + restore, Escape-to-close, click-outside-to-close.
export function Modal({ open, onClose, title, children, footer, size = "sm" }: ModalProps) {
  const [mounted, setMounted] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => setMounted(true), []);
  useFocusTrap(containerRef, open, onClose);

  if (!mounted || !open) return null;

  return createPortal(
    <div
      className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`bg-surface rounded-md border border-border w-full ${SIZE_CLASS[size]} overflow-hidden outline-none`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-3 py-2.5 border-b border-border">
          <span id={titleId} className="font-semibold text-sm text-gray-800 dark:text-gray-100">
            {title}
          </span>
          <button
            onClick={onClose}
            aria-label="Close dialog"
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-3">{children}</div>
        {footer && (
          <div className="px-3 py-2.5 border-t border-border bg-surface-raised">{footer}</div>
        )}
      </div>
    </div>,
    document.body
  );
}
