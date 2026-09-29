"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, type ReviewPriority, type ReviewQueueResponse } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime, formatPatientName, labelIsMrn } from "@/lib/format";
import { PriorityBadge, PRIORITY_META } from "@/components/reports/PriorityBadge";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { PenLine, RefreshCw } from "lucide-react";

type Tab = ReviewPriority | "all";
const TABS: Tab[] = ["all", "critical", "abnormal", "normal"];

function waiting(iso: string | null): string {
  if (!iso) return "—";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 60) return `${mins} min`;
  const hours = Math.round(mins / 60);
  return hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}

export default function ReviewQueuePage() {
  const router = useRouter();
  const { can } = useAuth();
  const [status, setStatus] = useState<"unsigned" | "signed">("unsigned");
  const [tab, setTab] = useState<Tab>("all");
  const [data, setData] = useState<ReviewQueueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.signoff.queue({ status }));
      setError(null);
    } catch (e: any) {
      setError(e.message || "Failed to load the review queue");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, [load]);

  const items = (data?.items ?? []).filter((i) => tab === "all" || i.priority === tab);
  const total = data ? data.counts.critical + data.counts.abnormal + data.counts.normal : 0;

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
        <div className="flex items-center gap-3">
          <PenLine className="w-7 h-7 text-primary-600" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Review Queue</h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {status === "unsigned"
                ? "Reports awaiting electronic signature — most urgent first, then longest waiting."
                : "Recently signed reports."}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {can("result.approve") && (
            <Link href="/review/ai" className="text-xs text-primary-600 hover:text-primary-700 font-medium px-2">
              AI confidence review →
            </Link>
          )}
          <div className="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 p-0.5 text-sm">
            {(["unsigned", "signed"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setStatus(s)}
                className={`px-3 py-1 rounded-md ${status === s
                  ? "bg-primary-600 text-white"
                  : "text-gray-600 dark:text-gray-300 hover:bg-black/5 dark:hover:bg-white/5"}`}
              >
                {s === "unsigned" ? "Unsigned" : "Signed"}
              </button>
            ))}
          </div>
          <button onClick={load} aria-label="Refresh" className="p-2 rounded-lg text-gray-500 hover:bg-black/5 dark:hover:bg-white/5">
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-4" />}

      <div className="flex flex-wrap gap-2 mb-4" role="tablist">
        {TABS.map((t) => {
          const count = t === "all" ? total : data?.counts[t] ?? 0;
          const active = tab === t;
          const color = t === "all" ? "" : PRIORITY_META[t].cls;
          return (
            <button
              key={t}
              role="tab"
              aria-selected={active}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 text-sm font-medium rounded-full border transition ${
                active
                  ? t === "all" ? "bg-gray-900 text-white border-gray-900 dark:bg-gray-100 dark:text-gray-900" : `${color} ring-2 ring-offset-1 ring-current`
                  : "border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-black/5 dark:hover:bg-white/5"
              }`}
            >
              {t === "all" ? "All" : PRIORITY_META[t].label}
              <span className="ml-1.5 text-xs opacity-70">{count}</span>
            </button>
          );
        })}
      </div>

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800 text-left text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
              <th className="py-3 px-4">Priority</th>
              <th className="py-3 px-4">Patient</th>
              <th className="py-3 px-4">Study</th>
              <th className="py-3 px-4">Why</th>
              <th className="py-3 px-4">{status === "unsigned" ? "Waiting" : "Signed"}</th>
              <th className="py-3 px-4">{status === "unsigned" ? "Reading" : "Signed by"}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {items.map((i) => (
              <tr
                key={i.study_instance_uid}
                onClick={() => router.push(`/study/${i.study_instance_uid}`)}
                onKeyDown={(e) => { if (e.key === "Enter") router.push(`/study/${i.study_instance_uid}`); }}
                tabIndex={0}
                className="cursor-pointer hover:bg-gray-50 dark:hover:bg-surface-raised focus:outline-none focus:bg-gray-50 dark:focus:bg-surface-raised"
              >
                <td className="py-2.5 px-4 whitespace-nowrap">
                  <PriorityBadge priority={i.priority} overridden={i.priority_overridden} />
                </td>
                <td className="py-2.5 px-4">
                  {labelIsMrn(i) ? (
                    <p className="font-medium font-mono text-gray-900 dark:text-gray-100">{i.patient_id || "—"}</p>
                  ) : (
                    <>
                      <p className="font-medium text-gray-900 dark:text-gray-100">{formatPatientName(i.patient_name) || "—"}</p>
                      <p className="text-xs text-gray-500 dark:text-gray-400 font-mono">{i.patient_id || ""}</p>
                    </>
                  )}
                </td>
                <td className="py-2.5 px-4">
                  <p className="text-gray-800 dark:text-gray-200">
                    {[i.modality, i.body_part_examined].filter(Boolean).join(" · ") || "—"}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[16rem]" title={i.study_description || ""}>
                    {i.study_description || i.usecases.map((u) => u.replace(/_/g, " ")).join(", ")}
                  </p>
                </td>
                <td className="py-2.5 px-4 text-xs text-gray-600 dark:text-gray-300 max-w-[22rem]">
                  {i.priority_reasons.length ? (
                    <span title={i.priority_reasons.join("\n")}>
                      {i.priority_reasons[0]}
                      {i.priority_reasons.length > 1 && <span className="text-gray-400"> +{i.priority_reasons.length - 1} more</span>}
                    </span>
                  ) : (
                    <span className="text-gray-400">No abnormal AI findings</span>
                  )}
                </td>
                <td className="py-2.5 px-4 whitespace-nowrap text-gray-600 dark:text-gray-300">
                  {status === "unsigned" ? waiting(i.received_at) : formatDateTime(i.signed_at)}
                </td>
                <td className="py-2.5 px-4 whitespace-nowrap text-xs text-gray-600 dark:text-gray-300">
                  {status === "unsigned"
                    ? (i.assigned_to_username ? `${i.reading_status.replace("_", " ")} · ${i.assigned_to_username}` : "Unassigned")
                    : i.signed_by || "—"}
                </td>
              </tr>
            ))}
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={6} className="py-12 text-center text-gray-400 dark:text-gray-500">
                  {status === "unsigned" ? "Nothing awaiting signature." : "No signed reports yet."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
