import clsx from "clsx";
import type { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={clsx("py-12 flex flex-col items-center gap-3 text-center", className)}>
      <span className="grid place-items-center w-14 h-14 rounded-2xl bg-accent/10 ring-1 ring-accent/20 text-accent float">
        <Icon className="w-7 h-7" />
      </span>
      <div>
        <p className="text-gray-500 dark:text-gray-400 font-medium">{title}</p>
        {description && <p className="text-gray-400 dark:text-gray-500 text-xs mt-1">{description}</p>}
      </div>
      {action}
    </div>
  );
}
