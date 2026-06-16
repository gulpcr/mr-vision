"use client";

import clsx from "clsx";

const SEVERITY: Record<string, { bg: string; text: string; label: string }> = {
  // Traditional QA flags
  missing_sequence:      { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  spacing_inconsistency: { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  incomplete_coverage:   { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  motion_artifact:       { bg: "bg-red-50",    text: "text-red-800",    label: "Error"   },
  low_resolution:        { bg: "bg-blue-50",   text: "text-blue-800",   label: "Info"    },
  slice_gap:             { bg: "bg-blue-50",   text: "text-blue-800",   label: "Info"    },
  // VLM-detected artifacts
  low_snr:                  { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  field_inhomogeneity:      { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  aliasing_artifact:        { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  susceptibility_artifact:  { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  truncation_artifact:      { bg: "bg-blue-50",   text: "text-blue-800",   label: "Info"    },
  chemical_shift_artifact:  { bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
  parallel_imaging_artifact:{ bg: "bg-yellow-50", text: "text-yellow-800", label: "Warning" },
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

function scoreColor(score: number) {
  if (score >= 8) return "text-green-700";
  if (score >= 5) return "text-yellow-700";
  return "text-red-700";
}

export function QAPanel({ flags, details }: QAPanelProps) {
  const vlmData = details?.vlm_qa as
    | { vlm_model: string; series_checked: number; per_series: Record<string, VLMSeries> }
    | undefined;

  if (flags.length === 0 && !vlmData) {
    return (
      <div className="rounded-lg bg-green-50 p-4">
        <p className="text-green-800 text-sm font-medium">No quality issues detected</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Flag badges */}
      {flags.length > 0 && (
        <div className="space-y-2">
          {flags.map((flag) => {
            const sev = SEVERITY[flag] || { bg: "bg-gray-50", text: "text-gray-800", label: "Info" };
            return (
              <div key={flag} className={clsx("rounded-lg p-3", sev.bg)}>
                <div className="flex items-center gap-2">
                  <span className={clsx("text-xs font-semibold px-2 py-0.5 rounded", sev.text)}>
                    {sev.label}
                  </span>
                  <span className={clsx("text-sm", sev.text)}>
                    {flag.replace(/_/g, " ")}
                  </span>
                </div>
                {details[flag] && (
                  <p className="mt-1 text-xs text-gray-600">
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
        <div className="rounded-lg border border-gray-200 bg-white p-3">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
            VLM Image Quality · {vlmData.series_checked} series checked
          </p>
          <div className="space-y-2">
            {Object.entries(vlmData.per_series).map(([file, s]) => (
              <div key={file} className="text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-gray-600 font-medium truncate max-w-[60%]">
                    {file.split("/").pop()}
                  </span>
                  <span className={clsx("font-semibold", scoreColor(s.quality_score))}>
                    {s.quality_score}/10
                  </span>
                </div>
                {s.assessment && (
                  <p className="text-gray-500 mt-0.5">{s.assessment}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
