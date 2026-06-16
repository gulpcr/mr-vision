"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, TrendData, TrendTimepoint } from "@/lib/api";
import { formatDate } from "@/lib/format";
import Link from "next/link";
import { ArrowLeft, TrendingUp, TrendingDown, Minus, AlertTriangle, Activity } from "lucide-react";

const RANO_CONFIG: Record<string, { label: string; bg: string; text: string; icon: React.ElementType }> = {
  CR: { label: "Complete Response", bg: "bg-green-100", text: "text-green-800", icon: TrendingDown },
  PR: { label: "Partial Response", bg: "bg-blue-100", text: "text-blue-800", icon: TrendingDown },
  SD: { label: "Stable Disease", bg: "bg-gray-100", text: "text-gray-700", icon: Minus },
  PD: { label: "Progressive Disease", bg: "bg-red-100", text: "text-red-800", icon: TrendingUp },
};

function flattenMeasurements(obj: Record<string, any>, prefix = ""): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      Object.assign(out, flattenMeasurements(v, key));
    } else if (typeof v === "number") {
      out[key] = v;
    }
  }
  return out;
}

function formatKey(key: string): string {
  return key.replace(/\./g, " / ").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function SparkLine({ values, width = 120, height = 40, color = "#3b82f6" }: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
}) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - ((v - min) / range) * (height - 4) - 2;
    return `${x},${y}`;
  });
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline
        points={pts.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {values.map((v, i) => {
        const x = (i / (values.length - 1)) * width;
        const y = height - ((v - min) / range) * (height - 4) - 2;
        return (
          <circle key={i} cx={x} cy={y} r="2.5" fill={color} />
        );
      })}
    </svg>
  );
}

export default function PatientTrendPage() {
  const params = useParams();
  const patientId = decodeURIComponent(params.patientId as string);
  const usecase = params.usecase as string;

  const [trendData, setTrendData] = useState<TrendData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.trend
      .getPatient(patientId, usecase)
      .then(setTrendData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [patientId, usecase]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Activity className="w-6 h-6 text-blue-500 animate-pulse mr-2" />
        <p className="text-gray-500">Loading longitudinal data...</p>
      </div>
    );
  }

  if (error || !trendData) {
    return (
      <div className="space-y-4">
        <Link href="/worklist" className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700">
          <ArrowLeft className="w-4 h-4" /> Back to Worklist
        </Link>
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-6 text-center">
          <AlertTriangle className="w-8 h-8 text-red-400 mx-auto mb-2" />
          <p className="text-red-700 font-medium">No longitudinal data available</p>
          <p className="text-red-500 text-sm mt-1">{error || "No prior studies found for this patient."}</p>
        </div>
      </div>
    );
  }

  const { timepoints } = trendData;
  if (timepoints.length === 0) {
    return (
      <div className="space-y-4">
        <Link href="/worklist" className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700">
          <ArrowLeft className="w-4 h-4" /> Back to Worklist
        </Link>
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-6 text-center">
          <p className="text-amber-700">Only one study found. Longitudinal tracking requires at least 2 time points.</p>
        </div>
      </div>
    );
  }

  // Collect all numeric measurement keys from all timepoints
  const allMeasurementKeys = Array.from(
    new Set(
      timepoints.flatMap((tp) => Object.keys(flattenMeasurements(tp.measurements)))
    )
  );

  // Build time series for each measurement key
  const series: Record<string, { dates: string[]; values: number[] }> = {};
  for (const key of allMeasurementKeys) {
    const pts = timepoints
      .map((tp) => ({ date: tp.study_date || tp.created_at, value: flattenMeasurements(tp.measurements)[key] }))
      .filter((p) => p.value !== undefined && p.value !== null);
    if (pts.length >= 2) {
      series[key] = {
        dates: pts.map((p) => p.date),
        values: pts.map((p) => p.value),
      };
    }
  }

  const usecaseLabel = usecase.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="space-y-6">
      <Link href="/worklist" className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700">
        <ArrowLeft className="w-4 h-4" /> Back to Worklist
      </Link>

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Longitudinal Trend</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Patient ID: <span className="font-medium">{patientId}</span> &middot; {usecaseLabel} &middot;{" "}
          {timepoints.length} time point{timepoints.length !== 1 ? "s" : ""}
        </p>
      </div>

      {/* RANO Timeline */}
      {timepoints.some((tp) => tp.rano_classification) && (
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
          <h2 className="text-sm font-semibold text-gray-900 mb-4">RANO Response Timeline</h2>
          <div className="relative">
            <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-100" />
            <div className="space-y-4 pl-10">
              {timepoints.map((tp, i) => {
                const rano = tp.rano_classification;
                const conf = rano && RANO_CONFIG[rano] ? RANO_CONFIG[rano] : null;
                const Icon = conf?.icon || Minus;
                return (
                  <div key={tp.result_id} className="relative">
                    <div className={`absolute -left-6 top-1 w-3 h-3 rounded-full border-2 border-white ${
                      rano === "CR" ? "bg-green-400" :
                      rano === "PR" ? "bg-blue-400" :
                      rano === "SD" ? "bg-gray-300" :
                      rano === "PD" ? "bg-red-400" : "bg-gray-200"
                    }`} />
                    <div className={`flex items-center gap-3 p-3 rounded-lg border ${
                      conf ? `${conf.bg} border-opacity-50` : "bg-gray-50 border-gray-100"
                    }`}>
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-semibold text-gray-900">
                            {formatDate(tp.study_date || tp.created_at)}
                          </span>
                          {i === 0 && (
                            <span className="text-xs bg-gray-200 text-gray-600 px-1.5 py-0.5 rounded font-medium">Baseline</span>
                          )}
                          {i === timepoints.length - 1 && timepoints.length > 1 && (
                            <span className="text-xs bg-blue-100 text-blue-600 px-1.5 py-0.5 rounded font-medium">Latest</span>
                          )}
                        </div>
                      </div>
                      {conf && rano && (
                        <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg ${conf.bg}`}>
                          <Icon className={`w-4 h-4 ${conf.text}`} />
                          <span className={`text-xs font-bold ${conf.text}`}>{rano}</span>
                          <span className={`text-xs ${conf.text} hidden md:inline`}>&nbsp;{conf.label}</span>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* Measurement Time Series */}
      {Object.keys(series).length > 0 ? (
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
          <h2 className="text-sm font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-blue-500" />
            Measurement Trends
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {Object.entries(series).map(([key, { dates, values }]) => {
              const first = values[0];
              const last = values[values.length - 1];
              const changePct = first !== 0 ? ((last - first) / Math.abs(first)) * 100 : 0;
              const isUp = changePct > 1;
              const isDown = changePct < -1;
              return (
                <div key={key} className="border border-gray-100 rounded-lg p-4">
                  <div className="flex items-start justify-between mb-2">
                    <div>
                      <p className="text-xs font-semibold text-gray-600">{formatKey(key)}</p>
                      <p className="text-lg font-bold text-gray-900 mt-0.5">
                        {last % 1 === 0 ? last : last.toFixed(2)}
                      </p>
                    </div>
                    <div className={`flex items-center gap-1 text-xs font-semibold px-2 py-1 rounded-lg ${
                      isUp ? "bg-red-100 text-red-600" :
                      isDown ? "bg-green-100 text-green-600" :
                      "bg-gray-100 text-gray-500"
                    }`}>
                      {isUp ? <TrendingUp className="w-3 h-3" /> : isDown ? <TrendingDown className="w-3 h-3" /> : <Minus className="w-3 h-3" />}
                      {Math.abs(changePct).toFixed(1)}%
                    </div>
                  </div>
                  <SparkLine values={values} color={isUp ? "#ef4444" : isDown ? "#22c55e" : "#94a3b8"} />
                  <div className="flex justify-between mt-2">
                    <span className="text-xs text-gray-400">{formatDate(dates[0])}</span>
                    <span className="text-xs text-gray-400">{formatDate(dates[dates.length - 1])}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-8 text-center">
          <p className="text-gray-400 text-sm">Not enough numeric measurements to display trend charts.</p>
        </div>
      )}

      {/* Data Table */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-900">Raw Timepoint Data</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-100">
                <th className="text-left px-5 py-3 text-xs font-semibold text-gray-500 uppercase whitespace-nowrap">Date</th>
                {timepoints.some((tp) => tp.rano_classification) && (
                  <th className="text-left px-5 py-3 text-xs font-semibold text-gray-500 uppercase">RANO</th>
                )}
                {allMeasurementKeys.slice(0, 6).map((k) => (
                  <th key={k} className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase whitespace-nowrap">
                    {formatKey(k).split(" / ").pop()}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {timepoints.map((tp, i) => {
                const flat = flattenMeasurements(tp.measurements);
                return (
                  <tr key={tp.result_id} className={`hover:bg-gray-50 ${i === 0 ? "bg-blue-50/30" : ""}`}>
                    <td className="px-5 py-3 font-medium text-gray-900 whitespace-nowrap">
                      {formatDate(tp.study_date || tp.created_at)}
                      {i === 0 && <span className="ml-2 text-xs text-blue-500 font-medium">Baseline</span>}
                    </td>
                    {timepoints.some((tp2) => tp2.rano_classification) && (
                      <td className="px-5 py-3">
                        {tp.rano_classification ? (
                          <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                            RANO_CONFIG[tp.rano_classification]?.bg || "bg-gray-100"
                          } ${RANO_CONFIG[tp.rano_classification]?.text || "text-gray-700"}`}>
                            {tp.rano_classification}
                          </span>
                        ) : <span className="text-gray-300">—</span>}
                      </td>
                    )}
                    {allMeasurementKeys.slice(0, 6).map((k) => (
                      <td key={k} className="px-4 py-3 text-right text-gray-700">
                        {flat[k] !== undefined ? (
                          flat[k] % 1 === 0 ? flat[k] : flat[k].toFixed(2)
                        ) : (
                          <span className="text-gray-300">—</span>
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
