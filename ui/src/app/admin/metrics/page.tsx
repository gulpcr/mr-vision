"use client";

import { useEffect, useState } from "react";
import { api, QaMetrics } from "@/lib/api";
import {
  BarChart3, Clock, CheckCircle, AlertTriangle, TrendingUp, TrendingDown,
  Minus, RefreshCw, Filter
} from "lucide-react";

const USECASE_LABELS: Record<string, string> = {
  brain_mri: "Brain MRI",
  spine_mri: "Spine MRI",
  chest_mri: "Chest MRI",
  abdomen_mri: "Abdomen MRI",
};

function MetricCard({
  label,
  value,
  unit,
  icon: Icon,
  color,
  sub,
}: {
  label: string;
  value: string | number;
  unit?: string;
  icon: React.ElementType;
  color: string;
  sub?: string;
}) {
  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">{label}</p>
          <p className="text-2xl font-bold text-gray-900 mt-1">
            {value}
            {unit && <span className="text-base font-medium text-gray-400 ml-1">{unit}</span>}
          </p>
          {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
        </div>
        <div className={`p-2.5 rounded-lg ${color}`}>
          <Icon className="w-5 h-5" />
        </div>
      </div>
    </div>
  );
}

function ProgressBar({ value, max, color = "bg-blue-500", label }: {
  value: number; max: number; color?: string; label?: string
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-3">
      {label && <span className="text-xs text-gray-600 w-28 truncate shrink-0">{label}</span>}
      <div className="flex-1 bg-gray-100 rounded-full h-2">
        <div className={`${color} h-2 rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-medium text-gray-700 w-10 text-right shrink-0">{value.toFixed(1)}</span>
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

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">QA Dashboard</h1>
          <p className="text-sm text-gray-500 mt-0.5">Radiologist agreement rates, turnaround times, and audit metrics</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={usecase}
            onChange={(e) => setUsecase(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
          >
            <option value="">All Use Cases</option>
            {Object.entries(USECASE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {loading && !metrics ? (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="bg-white rounded-xl border border-gray-200 p-5 animate-pulse">
              <div className="h-4 bg-gray-100 rounded w-20 mb-3" />
              <div className="h-8 bg-gray-100 rounded w-16" />
            </div>
          ))}
        </div>
      ) : metrics ? (
        <>
          {/* KPI Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <MetricCard
              label="Median TAT"
              value={metrics.tat_median_minutes.toFixed(1)}
              unit="min"
              icon={Clock}
              color="bg-blue-100 text-blue-600"
              sub="Median turnaround time"
            />
            <MetricCard
              label="P95 TAT"
              value={metrics.tat_p95_minutes.toFixed(1)}
              unit="min"
              icon={TrendingUp}
              color="bg-purple-100 text-purple-600"
              sub="95th percentile"
            />
            <MetricCard
              label="Correction Rate"
              value={metrics.correction_rate_pct.toFixed(1)}
              unit="%"
              icon={metrics.correction_rate_pct > 10 ? TrendingUp : TrendingDown}
              color={metrics.correction_rate_pct > 10 ? "bg-red-100 text-red-500" : "bg-green-100 text-green-600"}
              sub="Radiologist corrections"
            />
            <MetricCard
              label="QA Flag Rate"
              value={metrics.qa_flag_rate_pct.toFixed(1)}
              unit="%"
              icon={AlertTriangle}
              color={metrics.qa_flag_rate_pct > 15 ? "bg-amber-100 text-amber-600" : "bg-green-100 text-green-600"}
              sub="Studies with QA flags"
            />
          </div>

          {/* Throughput + Queue */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="md:col-span-2 bg-white rounded-xl shadow-sm border border-gray-200 p-5">
              <h2 className="text-sm font-semibold text-gray-900 mb-4 flex items-center gap-2">
                <Clock className="w-4 h-4 text-blue-500" />
                Turnaround Time Breakdown
              </h2>
              <div className="space-y-4">
                <ProgressBar
                  label="Median"
                  value={metrics.tat_median_minutes}
                  max={tatMaxMinutes}
                  color="bg-blue-400"
                />
                <ProgressBar
                  label="P75"
                  value={metrics.tat_p75_minutes}
                  max={tatMaxMinutes}
                  color="bg-blue-500"
                />
                <ProgressBar
                  label="P95"
                  value={metrics.tat_p95_minutes}
                  max={tatMaxMinutes}
                  color="bg-purple-500"
                />
              </div>
              <div className="mt-6">
                <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">Median TAT by Use Case (min)</h3>
                <div className="space-y-3">
                  {Object.entries(metrics.tat_by_usecase).map(([uc, tat]) => (
                    <ProgressBar
                      key={uc}
                      label={USECASE_LABELS[uc] || uc}
                      value={tat}
                      max={tatMaxMinutes}
                      color="bg-indigo-400"
                    />
                  ))}
                  {Object.keys(metrics.tat_by_usecase).length === 0 && (
                    <p className="text-sm text-gray-400">No data for selected period</p>
                  )}
                </div>
              </div>
            </div>

            <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
              <h2 className="text-sm font-semibold text-gray-900 mb-4 flex items-center gap-2">
                <BarChart3 className="w-4 h-4 text-purple-500" />
                Review Queue
              </h2>
              <div className="space-y-3">
                {Object.entries(metrics.review_queue_stats).map(([status, count]) => (
                  <div key={status} className="flex items-center justify-between py-2 border-b border-gray-50">
                    <div className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full ${
                          status === "pending" ? "bg-amber-400" :
                          status === "approved" ? "bg-green-400" :
                          status === "corrected" ? "bg-red-400" : "bg-gray-300"
                        }`}
                      />
                      <span className="text-sm text-gray-700 capitalize">{status}</span>
                    </div>
                    <span className="text-sm font-bold text-gray-900">{count}</span>
                  </div>
                ))}
                {Object.keys(metrics.review_queue_stats).length === 0 && (
                  <p className="text-sm text-gray-400">Review queue is empty</p>
                )}
              </div>
              {reviewTotal > 0 && (
                <div className="mt-4 pt-3 border-t border-gray-100">
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-500">Total reviewed</span>
                    <span className="font-semibold text-gray-900">{reviewTotal}</span>
                  </div>
                  <div className="flex justify-between text-sm mt-1">
                    <span className="text-gray-500">Correction rate</span>
                    <span className={`font-semibold ${metrics.correction_rate_pct > 10 ? "text-red-600" : "text-green-600"}`}>
                      {metrics.correction_rate_pct.toFixed(1)}%
                    </span>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Job Stats */}
          <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
            <h2 className="text-sm font-semibold text-gray-900 mb-4 flex items-center gap-2">
              <CheckCircle className="w-4 h-4 text-green-500" />
              Job Completion Statistics ({days}-day window)
            </h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="text-center p-4 bg-green-50 rounded-lg">
                <p className="text-2xl font-bold text-green-700">{metrics.jobs_completed}</p>
                <p className="text-xs text-green-600 mt-1">Completed</p>
              </div>
              <div className="text-center p-4 bg-red-50 rounded-lg">
                <p className="text-2xl font-bold text-red-700">{metrics.jobs_failed}</p>
                <p className="text-xs text-red-600 mt-1">Failed</p>
              </div>
              <div className="text-center p-4 bg-blue-50 rounded-lg">
                <p className="text-2xl font-bold text-blue-700">
                  {metrics.jobs_completed + metrics.jobs_failed > 0
                    ? (
                        (metrics.jobs_completed /
                          (metrics.jobs_completed + metrics.jobs_failed)) *
                        100
                      ).toFixed(1)
                    : "0"}%
                </p>
                <p className="text-xs text-blue-600 mt-1">Success Rate</p>
              </div>
              <div className="text-center p-4 bg-amber-50 rounded-lg">
                <p className="text-2xl font-bold text-amber-700">{metrics.qa_flag_rate_pct.toFixed(1)}%</p>
                <p className="text-xs text-amber-600 mt-1">QA Flag Rate</p>
              </div>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
