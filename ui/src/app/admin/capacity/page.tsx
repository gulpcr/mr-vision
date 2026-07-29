"use client";

import { useEffect, useState } from "react";
import { api, CapacityMetrics } from "@/lib/api";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { EmptyState } from "@/components/ui/EmptyState";
import { BarChart2, TrendingUp, Clock, RefreshCw, Cpu } from "lucide-react";

const USECASE_COLORS: Record<string, string> = {
  brain_mri: "bg-blue-500",
  spine_mri: "bg-purple-500",
  chest_mri: "bg-teal-500",
  abdomen_mri: "bg-orange-500",
};

const USECASE_LABELS: Record<string, string> = {
  brain_mri: "Brain MRI",
  spine_mri: "Spine MRI",
  chest_mri: "Chest MRI",
  abdomen_mri: "Abdomen MRI",
};

function HeatmapCell({ value, max }: { value: number; max: number }) {
  const intensity = max > 0 ? value / max : 0;
  const bg =
    intensity === 0 ? "bg-gray-50 dark:bg-gray-800 text-gray-300" :
    intensity < 0.25 ? "bg-blue-100 dark:bg-blue-900 text-blue-600 dark:text-blue-400" :
    intensity < 0.5 ? "bg-blue-300 text-blue-800" :
    intensity < 0.75 ? "bg-blue-500 text-white" :
    "bg-blue-700 text-white";
  return (
    <div className={`w-8 h-8 rounded flex items-center justify-center text-xs font-medium ${bg}`}>
      {value > 0 ? value : ""}
    </div>
  );
}

function BarGraph({ values, labels, color = "bg-blue-500", maxOverride }: {
  values: number[];
  labels: string[];
  color?: string;
  maxOverride?: number;
}) {
  const max = maxOverride ?? Math.max(...values, 1);
  // Show at most ~10 x-axis labels so 30-day charts don't crowd.
  const labelEvery = Math.max(1, Math.ceil(values.length / 10));
  return (
    <div>
      {/* Bars — each column is h-full so the percentage height resolves against
          a definite-height parent (the h-32 row). */}
      <div className="flex items-end gap-1 h-32">
        {values.map((v, i) => (
          <div key={i} className="flex-1 h-full flex items-end group relative">
            <div
              className={`w-full ${color} rounded-sm transition-all opacity-80 hover:opacity-100`}
              style={{ height: `${(v / max) * 100}%`, minHeight: v > 0 ? "3px" : "0" }}
            />
            <div className="absolute -top-7 left-1/2 -translate-x-1/2 bg-gray-800 text-white text-xs px-1.5 py-0.5 rounded opacity-0 group-hover:opacity-100 pointer-events-none whitespace-nowrap z-10">
              {labels[i]}: {v}
            </div>
          </div>
        ))}
      </div>
      {/* X-axis labels */}
      <div className="flex gap-1 mt-1.5">
        {labels.map((l, i) => (
          <span key={i} className="flex-1 text-center text-gray-400 dark:text-gray-500 truncate" style={{ fontSize: "9px" }}>
            {i % labelEvery === 0 ? l : ""}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function CapacityPage() {
  const [metrics, setMetrics] = useState<CapacityMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(30);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.metrics.getCapacity(days);
      setMetrics(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [days]);

  const heatmapMax = metrics ? Math.max(...metrics.hourly_heatmap, 1) : 1;
  const forecastMax = metrics
    ? Math.max(...metrics.forecast_7day, ...metrics.last_7days_actual, 1)
    : 1;

  const dailyTotals = metrics?.daily_volume.map((d) => d.total) ?? [];
  const dailyLabels = metrics?.daily_volume.map((d) =>
    new Date(d.date).toLocaleDateString("en-US", { month: "short", day: "numeric" })
  ) ?? [];

  const forecastLabels = metrics?.forecast_7day.map((_, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i + 1);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }) ?? [];

  const actualLabels = metrics?.last_7days_actual.map((_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - 6 + i);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }) ?? [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Capacity Analytics</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Scanner utilization, volume trends, and 7-day demand forecast</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="text-sm border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin motion-reduce:animate-none" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {loading && !metrics ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="bg-white dark:bg-surface rounded-xl border border-gray-200 dark:border-gray-700 p-5 animate-pulse motion-reduce:animate-none h-48" />
          ))}
        </div>
      ) : metrics ? (
        <>
          {/* KPI Row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase">Total Volume</p>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 mt-1">
                {dailyTotals.reduce((a, b) => a + b, 0)}
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">studies in {days} days</p>
            </div>
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase">Daily Average</p>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 mt-1">
                {dailyTotals.length > 0
                  ? (dailyTotals.reduce((a, b) => a + b, 0) / dailyTotals.length).toFixed(1)
                  : "0"}
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">studies per day</p>
            </div>
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <div className="flex items-center gap-2">
                <Clock className="w-4 h-4 text-amber-500 dark:text-amber-400" />
                <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase">Peak Hour</p>
              </div>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 mt-1">
                {String(metrics.peak_hour).padStart(2, "0")}:00
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">highest study volume</p>
            </div>
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <div className="flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-blue-500 dark:text-blue-400" />
                <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase">7-Day Forecast</p>
              </div>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 mt-1">
                {metrics.forecast_7day.reduce((a, b) => a + b, 0).toFixed(0)}
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">predicted studies</p>
            </div>
          </div>

          {/* Daily Volume Chart */}
          <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
              <BarChart2 className="w-4 h-4 text-blue-500 dark:text-blue-400" />
              Daily Study Volume ({days} days)
            </h2>
            {dailyTotals.length > 0 ? (
              <BarGraph values={dailyTotals} labels={dailyLabels} color="bg-blue-500" />
            ) : (
              <p className="text-sm text-gray-400 dark:text-gray-500 text-center py-8">No data for selected period</p>
            )}
          </div>

          {/* Hourly Heatmap */}
          <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center gap-2">
              <Clock className="w-4 h-4 text-amber-500 dark:text-amber-400" />
              Hourly Activity Heatmap
            </h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">Average studies submitted per hour of day</p>
            <div className="flex items-center gap-1 flex-wrap">
              {metrics.hourly_heatmap.map((v, h) => (
                <div key={h} className="flex flex-col items-center gap-1">
                  <HeatmapCell value={v} max={heatmapMax} />
                  <span className="text-xs text-gray-400 dark:text-gray-500" style={{ fontSize: "9px" }}>
                    {String(h).padStart(2, "0")}
                  </span>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-3 mt-3">
              <span className="text-xs text-gray-400 dark:text-gray-500">Low</span>
              <div className="flex gap-1">
                {["bg-gray-50 dark:bg-gray-800", "bg-blue-100 dark:bg-blue-900", "bg-blue-300", "bg-blue-500", "bg-blue-700"].map((c, i) => (
                  <div key={i} className={`w-5 h-3 rounded ${c}`} />
                ))}
              </div>
              <span className="text-xs text-gray-400 dark:text-gray-500">High</span>
            </div>
          </div>

          {/* 7-day Forecast vs Actual */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-blue-500 dark:text-blue-400" />
                Last 7 Days — Actual
              </h2>
              <BarGraph
                values={metrics.last_7days_actual}
                labels={actualLabels}
                color="bg-blue-400"
                maxOverride={forecastMax}
              />
            </div>
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-teal-500" />
                Next 7 Days — Forecast
                <span className="text-xs text-gray-400 dark:text-gray-500 font-normal">(exponential smoothing)</span>
              </h2>
              <BarGraph
                values={metrics.forecast_7day}
                labels={forecastLabels}
                color="bg-teal-400"
                maxOverride={forecastMax}
              />
            </div>
          </div>

          {/* Avg Duration by Use Case */}
          {Object.keys(metrics.avg_duration_by_usecase).length > 0 && (
            <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <Cpu className="w-4 h-4 text-purple-500 dark:text-purple-400" />
                Average Processing Duration by Use Case
              </h2>
              <div className="space-y-3">
                {Object.entries(metrics.avg_duration_by_usecase).map(([uc, minutes]) => {
                  const maxMin = Math.max(...Object.values(metrics.avg_duration_by_usecase), 1);
                  const pct = (minutes / maxMin) * 100;
                  const colorClass = USECASE_COLORS[uc] || "bg-gray-400";
                  return (
                    <div key={uc} className="flex items-center gap-3">
                      <span className="text-sm text-gray-600 dark:text-gray-400 w-28 shrink-0">
                        {USECASE_LABELS[uc] || uc}
                      </span>
                      <div className="flex-1 bg-gray-100 dark:bg-gray-800 rounded-full h-2.5">
                        <div
                          className={`${colorClass} h-2.5 rounded-full transition-all`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className="text-sm font-medium text-gray-700 dark:text-gray-300 w-16 text-right shrink-0">
                        {minutes.toFixed(1)} min
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </>
      ) : (
        <EmptyState icon={BarChart2} title="No capacity data available" description="Try refreshing or selecting a different time range." />
      )}
    </div>
  );
}
