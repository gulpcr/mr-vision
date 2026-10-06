"use client";

import clsx from "clsx";
import { ArrowRight, Ban, X } from "lucide-react";
import { ApiError } from "@/lib/api";

interface ActionBlockedNoticeProps {
  /** What the user tried to do, e.g. "Share link not created". */
  title: string;
  /** The thrown error; an ApiError with a remedy gets the full explanation. */
  error: unknown;
  onDismiss?: () => void;
  className?: string;
}

/** An action the server refused on purpose (409): why it was refused, how to make it
 * work, and a link to where that is done. Plain errors show just their message. */
export function ActionBlockedNotice({ title, error, onDismiss, className }: ActionBlockedNoticeProps) {
  const api = error instanceof ApiError ? error : null;
  const message = error instanceof Error ? error.message : String(error ?? "");
  return (
    <div
      role="alert"
      className={clsx(
        "flex items-start gap-3 rounded-lg border px-4 py-3",
        "bg-amber-50 border-amber-200 dark:bg-amber-950/60 dark:border-amber-900",
        className,
      )}
    >
      <Ban className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0 space-y-1.5">
        <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">{title}</p>
        <p className="text-sm text-amber-900/90 dark:text-amber-100/90 break-words">
          <span className="font-medium">Why: </span>{message}
        </p>
        {api?.remedy && (
          <p className="text-sm text-amber-900/90 dark:text-amber-100/90 break-words">
            <span className="font-medium">How to fix: </span>{api.remedy}
          </p>
        )}
        {api?.action?.path && (
          <a
            href={api.action.path}
            onClick={onDismiss}
            className="inline-flex items-center gap-1 text-sm font-semibold text-amber-800 dark:text-amber-300 hover:underline"
          >
            {api.action.label} <ArrowRight className="w-3.5 h-3.5" />
          </a>
        )}
      </div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          aria-label="Dismiss"
          className="text-amber-500 hover:text-amber-700 dark:hover:text-amber-300 shrink-0"
        >
          <X className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
