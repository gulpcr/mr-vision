import clsx from "clsx";
import { ChevronDown, ChevronUp } from "lucide-react";

export function Table({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={clsx("w-full text-sm", className)}>{children}</table>
    </div>
  );
}

/** sr-only table caption — every DataTable needs one for screen-reader context. */
export function Caption({ children }: { children: React.ReactNode }) {
  return <caption className="sr-only">{children}</caption>;
}

export function Th({ children, className, ...rest }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={clsx(
        "text-left py-2 px-3 text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider",
        className
      )}
      {...rest}
    >
      {children}
    </th>
  );
}

interface SortableThProps {
  label: string;
  field: string;
  activeField: string;
  dir: "asc" | "desc";
  onSort: (field: string) => void;
  className?: string;
}

export function SortableTh({ label, field, activeField, dir, onSort, className }: SortableThProps) {
  const active = activeField === field;
  const ariaSort: "ascending" | "descending" | "none" = active ? (dir === "asc" ? "ascending" : "descending") : "none";
  return (
    <th scope="col" aria-sort={ariaSort} className={clsx("text-left py-2 px-3", className)}>
      <button
        type="button"
        onClick={() => onSort(field)}
        className="flex items-center gap-1 text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-200 select-none"
      >
        {label}
        {!active ? (
          <ChevronDown className="w-3 h-3 text-gray-300" />
        ) : dir === "asc" ? (
          <ChevronUp className="w-3 h-3 text-primary-500" />
        ) : (
          <ChevronDown className="w-3 h-3 text-primary-500" />
        )}
      </button>
    </th>
  );
}
