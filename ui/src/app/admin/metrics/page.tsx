"use client";

import { useEffect, useState } from "react";
import { api, QaMetrics } from "@/lib/api";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { EmptyState } from "@/components/ui/EmptyState";
import { CountUp } from "@/components/ui/CountUp";
import { Donut, type DonutSegment } from "@/components/ui/Charts";
import {
  BarChart3, Clock, CheckCircle, AlertTriangle, TrendingUp, TrendingDown,
  RefreshCw, PieChart, XCircle, Gauge,
} from "lucide-react";

const USECASE_LABELS: Record<string, string> = {
  brain_mri: "Brain MRI",
  spine_mri: "Spine MRI",
  chest_mri: "Chest MRI",
  abdomen_mri: "Abdomen MRI",
};

const DAY_RANGES = [7, 30, 90] as const;

// Reuses the same validated status triad as the dashboard's Pipeline Status
// donut (green/amber/red — passed the CVD-separation + normal-vision checks in
// both light and dark) so "good / pending / needs-attention" reads identically
// everywhere in the app. Unrecognised statuses fall back to neutral slate.
const REVIEW_STATUS_COLOR: Record<string, string> = {
  approved: "#10b981",
  pending: "#f59e0b",
  corrected: "#f43f5e",
};
const REVIEW_STATUS_FALLBACK = "#94a3b8";

function MetricCard({
  label,
  value,
  unit,
  icon: Icon,
  grad,
  sub,
}: {
  label: string;
  value: number;
  unit?: string;
  icon: React.ElementType;
  grad: string;
  sub?: string;
}) {
  return (
    <div className="group glass accent-top rounded-2xl p-5 hover-lift">
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">{label}</p>
          <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1.5 tabular-nums">
            <CountUp value={value} />
            {unit && <span className="text-base font-semibold text-gray-400 dark:text-gray-500 ml-1">{unit}</span>}
          </p>
          {sub && <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{sub}</p>}
        </div>
        <div className={`grid place-items-center w-12 h-12 rounded-xl shadow-glow-sm text-white bg-gradient-to-br ${grad} transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-3 shrink-0`}>
          <Icon className="w-6 h-6" />
        </div>
      </div>
    </div>
  );
}

function ProgressBar({ value, max, color = "bg-accent", label }: {
  value: number; max: number; color?: string; label?: string
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-3">
      {label && <span className="text-xs text-gray-500 dark:text-gray-400 w-28 truncate shrink-0">{label}</span>}
      <div className="flex-1 bg-gray-100 dark:bg-white/5 rounded-full h-2">
        <div className={`${color} h-2 rounded-full transition-all duration-700`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-semibold text-gray-700 dark:text-gray-300 w-10 text-right shrink-0 tabular-nums">{value.toFixed(1)}</span>
    </div>
  );
}

// Skeleton mirrors the real layout's shape (KPI row + 2-col row + job-stats row)
// so loading doesn't cause a jarring height jump once data arrives.
function DashboardSkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="glass rounded-2xl p-5 h-[104px] shimmer" />
        ))}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="md:col-span-2 glass rounded-2xl h-[280px] shimmer" />
        <div className="glass rounded-2xl h-[280px] shimmer" />
      </div>
      <div className="glass rounded-2xl h-[140px] shimmer" />
    </div>
  );
}

export default function QaDashboardPage() {
  const [metrics, setMetrics] = useState<QaMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(30);
  const [usecase, setUsecase] = useState("");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.metrics.getQa(days, usecase || undefined);
      setMetrics(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [days, usecase]);

  const tatMaxMinutes = metrics
    ? Math.max(metrics.tat_median_minutes, metrics.tat_p75_minutes, metrics.tat_p95_minutes, 60)
    : 60;

  const reviewTotal = metrics
    ? Object.values(metrics.review_queue_stats).reduce((a, b) => a + b, 0)
    : 0;

  const reviewSegments: DonutSegment[] = metrics
    ? Object.entries(metrics.review_queue_stats).map(([status, count]) => ({
        label: status.charAt(0).toUpperCase() + status.slice(1),
        value: count,
        color: REVIEW_STATUS_COLOR[status] ?? REVIEW_STATUS_FALLBACK,
      }))
    : [];

  const jobsTotal = metrics ? metrics.jobs_completed + metrics.jobs_failed : 0;
  const successRate = jobsTotal > 0 ? (metrics!.jobs_completed / jobsTotal) * 100 : 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">
            <span className="text-gradient">QA Dashboard</span>
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Radiologist agreement rates, turnaround times, and audit metrics</p>
        </div>

        {/* Filter toolbar */}
        <div className="glass rounded-2xl px-3 py-2.5 flex items-center gap-2.5 flex-wrap">
          <select
            value={usecase}
            onChange={(e) => setUsecase(e.target.value)}
            className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition"
          >
            <option value="">All Use Cases</option>
            {Object.entries(USECASE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>

          {/* Day-range segmented control — same pattern as the dashboard's trend range */}
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
        <DashboardSkeleton />
      ) : metrics ? (
        <>
          {/* KPI Cards */}
          <div className="stagger grid grid-cols-2 md:grid-cols-4 gap-4">
            <MetricCard
              label="Median TAT"
              value={metrics.tat_median_minutes}
              unit="min"
              icon={Clock}
              grad="from-cyan-500 to-teal-600"
              sub="Median turnaround time"
            />
            <MetricCard
              label="P95 TAT"
              value={metrics.tat_p95_minutes}
              unit="min"
              icon={TrendingUp}
              grad="from-violet-500 to-indigo-600"
              sub="95th percentile"
            />
            <MetricCard
              label="Correction Rate"
              value={metrics.correction_rate_pct}
              unit="%"
              icon={metrics.correction_rate_pct > 10 ? TrendingUp : TrendingDown}
              grad={metrics.correction_rate_pct > 10 ? "from-red-500 to-rose-600" : "from-emerald-500 to-green-600"}
              sub="Radiologist corrections"
            />
            <MetricCard
              label="QA Flag Rate"
              value={metrics.qa_flag_rate_pct}
              unit="%"
              icon={AlertTriangle}
              grad={metrics.qa_flag_rate_pct > 15 ? "from-amber-500 to-orange-600" : "from-emerald-500 to-green-600"}
              sub="Studies with QA flags"
            />
          </div>

          {/* Throughput + Queue */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="md:col-span-2 glass rounded-2xl p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <Gauge className="w-4 h-4 text-accent" />
                Turnaround Time Breakdown
              </h2>
              <div className="space-y-4">
                <ProgressBar label="Median" value={metrics.tat_median_minutes} max={tatMaxMinutes} color="bg-cyan-500" />
                <ProgressBar label="P75" value={metrics.tat_p75_minutes} max={tatMaxMinutes} color="bg-teal-500" />
                <ProgressBar label="P95" value={metrics.tat_p95_minutes} max={tatMaxMinutes} color="bg-violet-500" />
              </div>
              <div className="mt-6">
                <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">Median TAT by Use Case (min)</h3>
                <div className="space-y-3">
                  {Object.entries(metrics.tat_by_usecase).map(([uc, tat]) => (
                    <ProgressBar key={uc} label={USECASE_LABELS[uc] || uc} value={tat} max={tatMaxMinutes} color="bg-indigo-400" />
                  ))}
                  {Object.keys(metrics.tat_by_usecase).length === 0 && (
                    <p className="text-sm text-gray-400 dark:text-gray-500">No data for selected period</p>
                  )}
                </div>
              </div>
            </div>

            <div className="glass rounded-2xl p-5">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <PieChart className="w-4 h-4 text-accent" />
                Review Queue
              </h2>
              {reviewTotal > 0 ? (
                <>
                  <Donut segments={reviewSegments} centerLabel="Studies" size={116} />
                  <div className="mt-4 pt-3 border-t border-border space-y-1">
                    <div className="flex justify-between text-sm">
                      <span className="text-gray-500 dark:text-gray-400">Total reviewed</span>
                      <span className="font-semibold text-gray-900 dark:text-gray-100 tabular-nums">{reviewTotal}</span>
                    </div>
                    <div className="flex justify-between text-sm">
                      <span className="text-gray-500 dark:text-gray-400">Correction rate</span>
                      <span className={`font-semibold tabular-nums ${metrics.correction_rate_pct > 10 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}>
                        {metrics.correction_rate_pct.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                </>
              ) : (
                <p className="text-sm text-gray-400 dark:text-gray-500 py-8 text-center">Review queue is empty</p>
              )}
            </div>
          </div>

          {/* Job Stats */}
          <div className="glass rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
              <CheckCircle className="w-4 h-4 text-accent" />
              Job Completion Statistics ({days}-day window)
            </h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="flex items-center gap-3 p-4 rounded-xl bg-black/[0.02] dark:bg-white/[0.03]">
                <span className="grid place-items-center w-10 h-10 rounded-xl text-white shadow-glow-sm bg-gradient-to-br from-emerald-500 to-green-600 shrink-0">
                  <CheckCircle className="w-5 h-5" />
                </span>
                <div className="min-w-0">
                  <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 tabular-nums"><CountUp value={metrics.jobs_completed} /></p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">Completed</p>
                </div>
              </div>
              <div className="flex items-center gap-3 p-4 rounded-xl bg-black/[0.02] dark:bg-white/[0.03]">
                <span className="grid place-items-center w-10 h-10 rounded-xl text-white shadow-glow-sm bg-gradient-to-br from-red-500 to-rose-600 shrink-0">
                  <XCircle className="w-5 h-5" />
                </span>
                <div className="min-w-0">
                  <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 tabular-nums"><CountUp value={metrics.jobs_failed} /></p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">Failed</p>
                </div>
              </div>
              <div className="flex items-center gap-3 p-4 rounded-xl bg-black/[0.02] dark:bg-white/[0.03]">
                <span className="grid place-items-center w-10 h-10 rounded-xl text-white shadow-glow-sm bg-gradient-to-br from-cyan-500 to-teal-600 shrink-0">
                  <TrendingUp className="w-5 h-5" />
                </span>
                <div className="min-w-0">
                  <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 tabular-nums">
                    <CountUp value={successRate} />%
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">Success Rate</p>
                </div>
              </div>
              <div className="flex items-center gap-3 p-4 rounded-xl bg-black/[0.02] dark:bg-white/[0.03]">
                <span className="grid place-items-center w-10 h-10 rounded-xl text-white shadow-glow-sm bg-gradient-to-br from-amber-500 to-orange-600 shrink-0">
                  <AlertTriangle className="w-5 h-5" />
                </span>
                <div className="min-w-0">
                  <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 tabular-nums">
                    <CountUp value={metrics.qa_flag_rate_pct} />%
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">QA Flag Rate</p>
                </div>
              </div>
            </div>
          </div>
        </>
      ) : (
        <EmptyState icon={BarChart3} title="No QA metrics available" description="Try refreshing or selecting a different time range." />
      )}
    </div>
  );
}
