import clsx from "clsx";
import { RefreshCw } from "lucide-react";

interface SpinnerProps {
  className?: string;
  /** Visible label rendered next to the spinner. When omitted, an sr-only "Loading" is used instead. */
  label?: string;
}

export function Spinner({ className, label }: SpinnerProps) {
  return (
    <span className="inline-flex items-center gap-2" role="status">
      <RefreshCw className={clsx("w-4 h-4 animate-spin motion-reduce:animate-none text-gray-400", className)} />
      {label ? <span className="text-sm text-gray-500 dark:text-gray-400">{label}</span> : <span className="sr-only">Loading</span>}
    </span>
  );
}
