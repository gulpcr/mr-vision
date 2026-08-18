"use client";

import { useEffect, useState } from "react";
import { api, CapacityMetrics } from "@/lib/api";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { EmptyState } from "@/components/ui/EmptyState";
import { CountUp } from "@/components/ui/CountUp";
import { BarChart2, TrendingUp, Clock, RefreshCw, Cpu, Activity } from "lucide-react";

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

const DAY_RANGES = [7, 30, 90] as const;

// Sequential single-hue ramp (brand accent) — the correct dataviz form for a
// magnitude heatmap (light → dark = low → high), just re-hued to the app's
// cyan accent instead of generic blue.
function HeatmapCell({ value, max }: { value: number; max: number }) {
  const intensity = max > 0 ? value / max : 0;
  const bg =
    intensity === 0 ? "bg-gray-50 dark:bg-white/5 text-gray-300 dark:text-gray-600" :
    intensity < 0.25 ? "bg-accent/10 text-accent" :
    intensity < 0.5 ? "bg-accent/30 text-accent" :
    intensity < 0.75 ? "bg-accent/60 text-white" :
    "bg-accent text-white";
  return (
    <div className={`w-8 h-8 rounded-lg flex items-center justify-center text-xs font-medium transition-colors ${bg}`}>
      {value > 0 ? value : ""}
    </div>
  );
}

function BarGraph({ values, labels, color = "bg-accent", maxOverride }: {
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
              className={`w-full ${color} rounded-t-sm transition-all opacity-80 hover:opacity-100`}
              style={{ height: `${(v / max) * 100}%`, minHeight: v > 0 ? "3px" : "0" }}
            />
            <div className="absolute -top-8 left-1/2 -translate-x-1/2 glass-raised text-gray-900 dark:text-gray-100 text-xs font-medium px-2 py-1 rounded-lg shadow-glow-sm opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none whitespace-nowrap z-10">
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

// Skeleton mirrors the real layout's shape (KPI row + volume chart + heatmap +
// 2-col forecast row + duration panel) so loading doesn't cause a height jump.
function CapacitySkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="glass rounded-2xl p-5 h-[104px] shimmer" />
        ))}
      </div>
      <div className="glass rounded-2xl h-[220px] shimmer" />
      <div className="glass rounded-2xl h-[180px] shimmer" />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="glass rounded-2xl h-[200px] shimmer" />
        <div className="glass rounded-2xl h-[200px] shimmer" />
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

  const totalVolume = dailyTotals.reduce((a, b) => a + b, 0);
  const dailyAverage = dailyTotals.length > 0 ? totalVolume / dailyTotals.length : 0;
  const forecastTotal = metrics ? Math.round(metrics.forecast_7day.reduce((a, b) => a + b, 0)) : 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">
            <span className="text-gradient">Capacity Analytics</span>
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Scanner utilization, volume trends, and 7-day demand forecast</p>
        </div>

        {/* Filter toolbar */}
        <div className="glass rounded-2xl px-3 py-2.5 flex items-center gap-2.5 flex-wrap">
          <div className="flex items-center gap-0.5 p-0.5 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-white/5 text-sm">
            {DAY_RANGES.map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`press px-3 py-1.5 rounded-md font-medium transition-all ${
                  days === d
                    ? "bg-primary-600 text-white shadow-glow-sm"
                    : "text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
                }`}
              >
                {d}d
              </button>
            ))}
          </div>
          <button
            onClick={load}
            disabled={loading}
            aria-label="Refresh"
            title="Refresh"
            className="press p-2.5 text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-accent/5 transition-colors disabled:opacity-40"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin motion-reduce:animate-none" : ""}`} />
          </button>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {loading && !metrics ? (
        <CapacitySkeleton />
      ) : metrics ? (
        <>
          {/* KPI Row */}
          <div className="stagger grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="group glass accent-top rounded-2xl p-5 hover-lift">
              <div className="flex items-start justify-between">
                <div className="min-w-0">
                  <p className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Total Volume</p>
                  <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1.5 tabular-nums"><CountUp value={totalVolume} /></p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">studies in {days} days</p>
                </div>
                <div className="grid place-items-center w-12 h-12 rounded-xl shadow-glow-sm text-white bg-gradient-to-br from-cyan-500 to-teal-600 transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-3 shrink-0">
                  <Activity className="w-6 h-6" />
                </div>
              </div>
            </div>
            <div className="group glass accent-top rounded-2xl p-5 hover-lift">
              <div className="flex items-start justify-between">
                <div className="min-w-0">
                  <p className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Daily Average</p>
                  <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1.5 tabular-nums"><CountUp value={dailyAverage} /></p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">studies per day</p>
                </div>
                <div className="grid place-items-center w-12 h-12 rounded-xl shadow-glow-sm text-white bg-gradient-to-br from-indigo-500 to-blue-600 transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-3 shrink-0">
                  <BarChart2 className="w-6 h-6" />
                </div>
              </div>
            </div>
            <div className="group glass accent-top rounded-2xl p-5 hover-lift">
              <div className="flex items-start justify-between">
                <div className="min-w-0">
                  <p className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Peak Hour</p>
                  <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1.5 tabular-nums">
                    {String(metrics.peak_hour).padStart(2, "0")}:00
                  </p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">highest study volume</p>
                </div>
                <div className="grid place-items-center w-12 h-12 rounded-xl shadow-glow-sm text-white bg-gradient-to-br from-amber-500 to-orange-600 transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-3 shrink-0">
                  <Clock className="w-6 h-6" />
                </div>
              </div>
            </div>
            <div className="group glass accent-top rounded-2xl p-5 hover-lift">
              <div className="flex items-start justify-between">
                <div className="min-w-0">
                  <p className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">7-Day Forecast</p>
                  <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1.5 tabular-nums"><CountUp value={forecastTotal} /></p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">predicted studies</p>
                </div>
                <div className="grid place-items-center w-12 h-12 rounded-xl shadow-glow-sm text-white bg-gradient-to-br from-violet-500 to-indigo-600 transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-3 shrink-0">
                  <TrendingUp className="w-6 h-6" />
                </div>
              </div>
            </div>
          </div>

          {/* Daily Volume Chart */}
          <div className="glass rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
              <BarChart2 className="w-4 h-4 text-accent" />
              Daily Study Volume ({days} days)
            </h2>
            {dailyTotals.length > 0 ? (
              <BarGraph values={dailyTotals} labels={dailyLabels} color="bg-accent" />
            ) : (
              <p className="text-sm text-gray-400 dark:text-gray-500 text-center py-8">No data for selected period</p>
            )}
          </div>

          {/* Hourly Heatmap */}
          <div className="glass rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center gap-2">
              <Clock className="w-4 h-4 text-accent" />
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
                {["bg-gray-50 dark:bg-white/5", "bg-accent/10", "bg-accent/30", "bg-accent/60", "bg-accent"].map((c, i) => (
                  <div key={i} className={`w-5 h-3 rounded ${c}`} />
                ))}
              </div>
              <span className="text-xs text-gray-400 dark:text-gray-500">High</span>
            </div>
          </div>

          {/* 7-day Forecast vs Actual */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="glass rounded-2xl p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-accent" />
                Last 7 Days — Actual
              </h2>
              <BarGraph
                values={metrics.last_7days_actual}
                labels={actualLabels}
                color="bg-accent"
                maxOverride={forecastMax}
              />
            </div>
            <div className="glass rounded-2xl p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-accent-2" />
                Next 7 Days — Forecast
                <span className="text-xs text-gray-400 dark:text-gray-500 font-normal">(exponential smoothing)</span>
              </h2>
              <BarGraph
                values={metrics.forecast_7day}
                labels={forecastLabels}
                color="bg-accent-2"
                maxOverride={forecastMax}
              />
            </div>
          </div>

          {/* Avg Duration by Use Case */}
          {Object.keys(metrics.avg_duration_by_usecase).length > 0 && (
            <div className="glass rounded-2xl p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <Cpu className="w-4 h-4 text-accent" />
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
                      <div className="flex-1 bg-gray-100 dark:bg-white/5 rounded-full h-2.5">
                        <div
                          className={`${colorClass} h-2.5 rounded-full transition-all duration-700`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className="text-sm font-semibold text-gray-700 dark:text-gray-300 w-16 text-right shrink-0 tabular-nums">
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
