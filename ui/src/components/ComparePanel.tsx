"use client";

import { useEffect, useState } from "react";
import { ComparisonData, Result } from "@/lib/api";
import { getPreviewUrl } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { QAPanel } from "./QAPanel";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  CheckCircle,
  AlertTriangle,
  Calendar,
} from "lucide-react";
import clsx from "clsx";

interface Props {
  data: ComparisonData;
  labelA?: string;
  labelB?: string;
}

function severityColor(severity: "low" | "medium" | "high", change: number) {
  if (severity === "low") return "text-green-600";
  if (severity === "medium") return "text-yellow-600";
  return "text-red-600";
}

function severityBg(severity: "low" | "medium" | "high") {
  if (severity === "low") return "bg-green-50";
  if (severity === "medium") return "bg-yellow-50";
  return "bg-red-50";
}

function DeltaIcon({ change, severity }: { change: number; severity: string }) {
  if (Math.abs(change) < 0.001) return <Minus className="w-3.5 h-3.5 text-gray-400" />;
  if (change > 0)
    return (
      <TrendingUp
        className={clsx(
          "w-3.5 h-3.5",
          severity === "low" ? "text-green-500" : severity === "medium" ? "text-yellow-500" : "text-red-500"
        )}
      />
    );
  return (
    <TrendingDown
      className={clsx(
        "w-3.5 h-3.5",
        severity === "low" ? "text-green-500" : severity === "medium" ? "text-yellow-500" : "text-red-500"
      )}
    />
  );
}

// Loads a JWT-protected image via fetch (with the Authorization header) and
// renders it from an object URL — a browser <img> never sends auth headers, so
// a plain src would 401 under jwt and show only the alt text.
function AuthImg({ src, alt, className }: { src: string; alt: string; className?: string }) {
  const [objUrl, setObjUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let revoke: string | null = null;
    setFailed(false);
    setObjUrl(null);
    const token = typeof window !== "undefined" ? localStorage.getItem("auth_token") : null;
    fetch(src, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.blob(); })
      .then((b) => { const u = URL.createObjectURL(b); revoke = u; setObjUrl(u); })
      .catch(() => setFailed(true));
    return () => { if (revoke) URL.revokeObjectURL(revoke); };
  }, [src]);

  if (failed)
    return (
      <div className="w-full h-80 flex items-center justify-center text-xs text-gray-400 bg-black rounded border border-gray-200">
        Not available
      </div>
    );
  if (!objUrl)
    return <div className="w-full h-80 bg-gray-900 rounded border border-gray-200 animate-pulse" />;
  /* eslint-disable-next-line @next/next/no-img-element */
  return <img src={objUrl} alt={alt} className={className} />;
}

function PreviewPair({
  uidA,
  uidB,
  usecase,
  view,
}: {
  uidA: string;
  uidB: string;
  usecase: string;
  view: "axial" | "coronal" | "sagittal";
}) {
  return (
    <div className="flex gap-2">
      <div className="flex-1">
        <p className="text-xs text-center text-gray-500 mb-1 capitalize">{view} A</p>
        <AuthImg
          src={getPreviewUrl(uidA, usecase, view)}
          alt={`${view} A`}
          className="w-full h-80 object-contain rounded border border-gray-200 bg-black"
        />
      </div>
      <div className="flex-1">
        <p className="text-xs text-center text-gray-500 mb-1 capitalize">{view} B</p>
        <AuthImg
          src={getPreviewUrl(uidB, usecase, view)}
          alt={`${view} B`}
          className="w-full h-80 object-contain rounded border border-gray-200 bg-black"
        />
      </div>
    </div>
  );
}

function formatKey(key: string) {
  return key
    .replace(/\./g, " › ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatNum(v: number) {
  if (Number.isInteger(v)) return v.toString();
  return v.toFixed(2);
}

export function ComparePanel({ data, labelA = "Study A", labelB = "Study B" }: Props) {
  const [activeView, setActiveView] = useState<"axial" | "coronal" | "sagittal">("axial");
  const { result_a, result_b, delta, usecase_name } = data;

  const measurementEntries = Object.entries(delta.measurements);
  const highCount = measurementEntries.filter(([, d]) => d.severity === "high").length;
  const medCount = measurementEntries.filter(([, d]) => d.severity === "medium").length;

  return (
    <div className="space-y-5">
      {/* Summary bar */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4 flex flex-wrap gap-6 items-center">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wider">Use Case</p>
          <p className="font-semibold text-gray-900 mt-0.5">
            {usecase_name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
          </p>
        </div>
        {delta.days_between !== null && (
          <div className="flex items-center gap-1.5 text-sm text-gray-600">
            <Calendar className="w-4 h-4 text-gray-400" />
            {delta.days_between === 0
              ? "Same day"
              : `${delta.days_between} day${delta.days_between !== 1 ? "s" : ""} apart`}
          </div>
        )}
        {highCount > 0 && (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full bg-red-50 text-red-700 text-xs font-medium">
            <AlertTriangle className="w-3.5 h-3.5" />
            {highCount} high-change metric{highCount > 1 ? "s" : ""}
          </span>
        )}
        {medCount > 0 && (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full bg-yellow-50 text-yellow-700 text-xs font-medium">
            <AlertTriangle className="w-3.5 h-3.5" />
            {medCount} medium-change metric{medCount > 1 ? "s" : ""}
          </span>
        )}
        {highCount === 0 && medCount === 0 && measurementEntries.length > 0 && (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full bg-green-50 text-green-700 text-xs font-medium">
            <CheckCircle className="w-3.5 h-3.5" />
            All changes within normal range
          </span>
        )}
      </div>

      {/* Result headers */}
      <div className="grid grid-cols-2 gap-4">
        {[
          { label: labelA, result: result_a },
          { label: labelB, result: result_b },
        ].map(({ label, result }) => (
          <div
            key={result.id}
            className="bg-white rounded-lg border border-gray-200 shadow-sm p-4"
          >
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">
              {label}
            </p>
            <p className="text-sm font-medium text-gray-900">{result.model_version}</p>
            <p className="text-xs text-gray-500 mt-0.5">
              {formatDateTime(result.created_at)}
              {result.version ? ` · v${result.version}` : ""}
            </p>
            {result.qa_flags.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {result.qa_flags.map((f) => (
                  <span
                    key={f}
                    className="px-1.5 py-0.5 rounded bg-yellow-50 text-yellow-700 text-xs"
                  >
                    {f.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Measurement delta table */}
      {measurementEntries.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-5 py-3 border-b border-gray-100">
            <h3 className="text-sm font-semibold text-gray-700">Measurement Comparison</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50">
                  <th className="text-left px-5 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Metric
                  </th>
                  <th className="text-right px-4 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    {labelA}
                  </th>
                  <th className="text-right px-4 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    {labelB}
                  </th>
                  <th className="text-right px-5 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Change
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {measurementEntries.map(([key, d]) => (
                  <tr
                    key={key}
                    className={clsx("transition-colors", severityBg(d.severity))}
                  >
                    <td className="px-5 py-3 font-medium text-gray-800">
                      {formatKey(key)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-600 tabular-nums">
                      {formatNum(d.a)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-900 font-medium tabular-nums">
                      {formatNum(d.b)}
                    </td>
                    <td className="px-5 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <DeltaIcon change={d.change} severity={d.severity} />
                        <span
                          className={clsx(
                            "font-semibold tabular-nums",
                            severityColor(d.severity, d.change)
                          )}
                        >
                          {d.change > 0 ? "+" : ""}
                          {formatNum(d.change)}
                        </span>
                        <span
                          className={clsx(
                            "text-xs tabular-nums",
                            severityColor(d.severity, d.change)
                          )}
                        >
                          ({d.change_pct > 0 ? "+" : ""}
                          {d.change_pct.toFixed(1)}%)
                        </span>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="px-5 py-2 border-t border-gray-100 bg-gray-50 flex gap-4 text-xs text-gray-500">
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-green-400 inline-block" /> &lt;10% change
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-yellow-400 inline-block" /> 10–25% change
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-red-400 inline-block" /> &gt;25% change
            </span>
          </div>
        </div>
      )}

      {/* QA flag diff */}
      {(delta.qa_flags_new.length > 0 || delta.qa_flags_resolved.length > 0) && (
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
          <h3 className="text-sm font-semibold text-gray-700 mb-3">QA Flag Changes</h3>
          <div className="flex flex-wrap gap-4">
            {delta.qa_flags_new.length > 0 && (
              <div>
                <p className="text-xs text-red-600 font-medium mb-1.5">New flags in {labelB}</p>
                <div className="flex flex-wrap gap-1.5">
                  {delta.qa_flags_new.map((f) => (
                    <span
                      key={f}
                      className="px-2 py-0.5 rounded-full bg-red-50 text-red-700 text-xs border border-red-100"
                    >
                      + {f.replace(/_/g, " ")}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {delta.qa_flags_resolved.length > 0 && (
              <div>
                <p className="text-xs text-green-600 font-medium mb-1.5">Resolved in {labelB}</p>
                <div className="flex flex-wrap gap-1.5">
                  {delta.qa_flags_resolved.map((f) => (
                    <span
                      key={f}
                      className="px-2 py-0.5 rounded-full bg-green-50 text-green-700 text-xs border border-green-100"
                    >
                      ✓ {f.replace(/_/g, " ")}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Preview images */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-5 py-3 border-b border-gray-100 flex items-center gap-3">
          <h3 className="text-sm font-semibold text-gray-700">Segmentation Overlays</h3>
          <div className="flex gap-1 ml-auto">
            {(["axial", "coronal", "sagittal"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setActiveView(v)}
                className={clsx(
                  "px-3 py-1 rounded text-xs font-medium transition-colors",
                  activeView === v
                    ? "bg-primary-600 text-white"
                    : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                )}
              >
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </button>
            ))}
          </div>
        </div>
        <div className="p-4">
          <PreviewPair
            uidA={result_a.study_instance_uid}
            uidB={result_b.study_instance_uid}
            usecase={usecase_name}
            view={activeView}
          />
        </div>
      </div>
    </div>
  );
}
