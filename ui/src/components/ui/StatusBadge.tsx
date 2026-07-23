"use client";

import clsx from "clsx";
import { Clock, CheckCircle, ShieldAlert } from "lucide-react";
import { useLocale } from "@/lib/i18n";
import type { Strings } from "@/lib/locales/en";

export type StatusBadgeVariant = "job" | "reading" | "alert-ack";

interface StatusBadgeProps {
  variant: StatusBadgeVariant;
  status: string;
  /** "reading" variant only: username of the radiologist currently assigned. */
  assignedTo?: string | null;
  /** "reading" variant only: pre-formatted sign-off date, e.g. "23/07/2026". */
  signedAt?: string | null;
  className?: string;
}

// Job-run status (Celery pipeline lifecycle). Colors unchanged from the
// previous components/StatusBadge.tsx — no visual regression on migration.
const JOB_STYLES: Record<string, string> = {
  pending: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200",
  routing: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  preprocessing: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  inferring: "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-300",
  postprocessing: "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300",
  completed: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  cancelled: "bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400",
};

// Reading/sign-off status. Canonical labels — the majority convention across
// the 4 narrative report components — now the single source of truth,
// replacing 6+ previously-diverging implementations (see ARCHITECTURE.md).
const READING_STYLES: Record<string, string> = {
  unread: "bg-gray-100 text-gray-600 border-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-600",
  in_progress: "bg-blue-100 text-blue-700 border-blue-300 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800",
  reported: "bg-amber-100 text-amber-800 border-amber-300 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800",
  signed: "bg-green-100 text-green-700 border-green-400 dark:bg-green-950 dark:text-green-300 dark:border-green-700",
};

function readingLabel(
  status: string,
  s: Strings["statusBadge"],
  assignedTo?: string | null,
  signedAt?: string | null
): string {
  switch (status) {
    case "unread":
      return s.unclaimed;
    case "in_progress":
      return assignedTo ? `${s.reading} — ${assignedTo}` : s.reading;
    case "reported":
      return assignedTo ? `${s.reported} — ${assignedTo}` : s.reported;
    case "signed":
      return `${s.signedOff}${assignedTo ? ` — ${assignedTo}` : ""}${signedAt ? ` · ${signedAt}` : ""}`;
    default:
      return status;
  }
}

const ALERT_ACK_CONFIG: Record<string, { label: string; cls: string; icon: typeof Clock }> = {
  pending: { label: "Pending", cls: "bg-yellow-50 text-yellow-700 dark:bg-yellow-950 dark:text-yellow-300", icon: Clock },
  acknowledged: { label: "Acknowledged", cls: "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300", icon: CheckCircle },
  escalated: { label: "Escalated", cls: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300", icon: ShieldAlert },
};

export function StatusBadge({ variant, status, assignedTo, signedAt, className }: StatusBadgeProps) {
  const { strings } = useLocale();

  if (variant === "job") {
    return (
      <span
        className={clsx(
          "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium",
          JOB_STYLES[status] || JOB_STYLES.pending,
          className
        )}
      >
        {status}
      </span>
    );
  }

  if (variant === "reading") {
    const isPreliminary = status !== "signed";
    return (
      <span
        className={clsx(
          "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-bold border",
          READING_STYLES[status] || READING_STYLES.unread,
          className
        )}
      >
        {isPreliminary ? `${strings.statusBadge.preliminary} · ` : ""}
        {readingLabel(status, strings.statusBadge, assignedTo, signedAt)}
      </span>
    );
  }

  // variant === "alert-ack"
  const entry = ALERT_ACK_CONFIG[status];
  const Icon = entry?.icon;
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 px-2 py-0.5 text-xs font-bold rounded-full",
        entry?.cls || "bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400",
        className
      )}
    >
      {Icon && <Icon className="w-3 h-3" />}
      {entry?.label || status}
    </span>
  );
}
