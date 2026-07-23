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
    <div className={clsx("py-10 flex flex-col items-center gap-2 text-center", className)}>
      <Icon className="w-10 h-10 text-gray-200 dark:text-gray-700" />
      <div>
        <p className="text-gray-500 dark:text-gray-400 font-medium">{title}</p>
        {description && <p className="text-gray-400 dark:text-gray-500 text-xs mt-1">{description}</p>}
      </div>
      {action}
    </div>
  );
}
