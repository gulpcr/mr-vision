"use client";

import clsx from "clsx";
import { SEVERITY_CONFIG, type SeverityTier } from "@/lib/design/severity";

// Maps each QA flag to the shared 5-tier severity scale (lib/design/severity.ts)
// instead of a locally-duplicated bg/text palette — one severity system app-wide.
const FLAG_SEVERITY: Record<string, SeverityTier> = {
  // Traditional QA flags
  missing_sequence: "moderate",
  spacing_inconsistency: "moderate",
  incomplete_coverage: "moderate",
  motion_artifact: "high",
  low_resolution: "informational",
  slice_gap: "informational",
  // VLM-detected artifacts
  low_snr: "moderate",
  field_inhomogeneity: "moderate",
  aliasing_artifact: "moderate",
  susceptibility_artifact: "moderate",
  truncation_artifact: "informational",
  chemical_shift_artifact: "moderate",
  parallel_imaging_artifact: "moderate",
};

interface VLMSeries {
  quality_score: number;
  artifacts: string[];
  assessment: string;
}

interface QAPanelProps {
  flags: string[];
  details: Record<string, any>;
}

function scoreTier(score: number): SeverityTier {
  if (score >= 8) return "good";
  if (score >= 5) return "moderate";
  return "high";
}

export function QAPanel({ flags, details }: QAPanelProps) {
  const vlmData = details?.vlm_qa as
    | { vlm_model: string; series_checked: number; per_series: Record<string, VLMSeries> }
    | undefined;

  if (flags.length === 0 && !vlmData) {
    const good = SEVERITY_CONFIG.good;
    const GoodIcon = good.icon;
    return (
      <div className={clsx("rounded-lg p-4 flex items-center gap-2", good.badgeClass)}>
        <GoodIcon className="w-4 h-4 shrink-0" />
        <p className="text-sm font-medium">No quality issues detected</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Flag badges */}
      {flags.length > 0 && (
        <div className="space-y-2">
          {flags.map((flag) => {
            const tier = FLAG_SEVERITY[flag] || "informational";
            const cfg = SEVERITY_CONFIG[tier];
            const Icon = cfg.icon;
            return (
              <div key={flag} className={clsx("rounded-lg p-3", cfg.badgeClass)}>
                <div className="flex items-center gap-2">
                  <span className="inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded">
                    <Icon className="w-3.5 h-3.5" /> {cfg.label}
                  </span>
                  <span className="text-sm">{flag.replace(/_/g, " ")}</span>
                </div>
                {details[flag] && (
                  <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">
                    {typeof details[flag] === "string"
                      ? details[flag]
                      : JSON.stringify(details[flag])}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* VLM per-series quality scores */}
      {vlmData && vlmData.series_checked > 0 && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-surface p-3">
          <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
            VLM Image Quality · {vlmData.series_checked} series checked
          </p>
          <div className="space-y-2">
            {Object.entries(vlmData.per_series).map(([file, s]) => (
              <div key={file} className="text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-gray-600 dark:text-gray-400 font-medium truncate max-w-[60%]">
                    {file.split("/").pop()}
                  </span>
                  <span className={clsx("font-semibold", SEVERITY_CONFIG[scoreTier(s.quality_score)].textClass)}>
                    {s.quality_score}/10
                  </span>
                </div>
                {s.assessment && (
                  <p className="text-gray-500 dark:text-gray-400 mt-0.5">{s.assessment}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
