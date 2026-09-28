import type { ReviewPriority } from "@/lib/api";
import { AlertOctagon, AlertTriangle, CheckCircle2 } from "lucide-react";

export const PRIORITY_META: Record<ReviewPriority, { label: string; cls: string; icon: typeof AlertOctagon }> = {
  critical: {
    label: "Critical",
    cls: "bg-red-50 text-red-700 border-red-200 dark:bg-red-950 dark:text-red-300 dark:border-red-900",
    icon: AlertOctagon,
  },
  abnormal: {
    label: "Abnormal",
    cls: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
    icon: AlertTriangle,
  },
  normal: {
    label: "Normal",
    cls: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900",
    icon: CheckCircle2,
  },
};

export function PriorityBadge({ priority, overridden = false }: { priority: ReviewPriority; overridden?: boolean }) {
  const meta = PRIORITY_META[priority];
  const Icon = meta.icon;
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full border ${meta.cls}`}
      title={overridden ? "Priority set manually by a radiologist" : "Priority computed from the AI findings"}
    >
      <Icon className="w-3.5 h-3.5" />
      {meta.label}
      {overridden && <span className="font-normal opacity-70">· manual</span>}
    </span>
  );
}
