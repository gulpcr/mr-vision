import clsx from "clsx";
import { Sparkles } from "lucide-react";
import { useLocale } from "@/lib/i18n";

interface AIProvenanceBannerProps {
  /** "footer": muted italic footnote (existing ReportView style). "callout": amber attention banner for content still in draft/non-diagnostic state (existing Mammography style). */
  variant?: "footer" | "callout";
  /** Extra provenance detail appended after the standard disclaimer, e.g. a model provider name. */
  additionalNote?: string;
  className?: string;
}

// The one shared AI-provenance disclaimer component — every AI-touched report
// surface must use this instead of a per-component copy (see redesign brief:
// "a single disclaimer string sourced from one shared component"). Text comes
// from the locale catalog (lib/locales), not a hardcoded constant.
export function AIProvenanceBanner({ variant = "footer", additionalNote, className }: AIProvenanceBannerProps) {
  const { strings } = useLocale();
  const disclaimer = strings.aiProvenance.disclaimer;

  if (variant === "callout") {
    return (
      <div
        className={clsx(
          "flex items-start gap-3 bg-amber-50 dark:bg-amber-950 border border-amber-300 dark:border-amber-800 rounded-lg px-4 py-2.5",
          className
        )}
      >
        <Sparkles className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
        <p className="text-xs text-amber-800 dark:text-amber-300 flex-1">
          <span className="font-semibold">AI-generated report (read-only).</span> {disclaimer}
          {additionalNote ? ` ${additionalNote}` : ""}
        </p>
      </div>
    );
  }

  return (
    <div
      className={clsx(
        "border-t border-gray-200 dark:border-gray-700 pt-3 text-xs text-gray-400 dark:text-gray-500 italic",
        className
      )}
    >
      DISCLAIMER: {disclaimer}
      {additionalNote ? ` ${additionalNote}` : ""}
    </div>
  );
}
