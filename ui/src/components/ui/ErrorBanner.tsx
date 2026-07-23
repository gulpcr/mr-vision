"use client";

import clsx from "clsx";
import { AlertTriangle, X } from "lucide-react";

interface ErrorBannerProps {
  title?: string;
  message: string;
  onDismiss?: () => void;
  className?: string;
}

export function ErrorBanner({ title = "Something went wrong", message, onDismiss, className }: ErrorBannerProps) {
  return (
    <div
      role="alert"
      className={clsx(
        "flex items-start gap-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-md px-3 py-2.5",
        className
      )}
    >
      <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-red-800 dark:text-red-300">{title}</p>
        <p className="text-xs text-red-700 dark:text-red-400 mt-0.5 break-words">{message}</p>
      </div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          aria-label="Dismiss error"
          className="text-red-400 hover:text-red-600 dark:hover:text-red-300 shrink-0"
        >
          <X className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
