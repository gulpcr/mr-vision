"use client";

import Link from "next/link";
import { AreaTrend, Donut } from "@/components/ui/Charts";
import type { WidgetData } from "@/lib/api";

// Categorical palette for donuts/bars (legend always carries the label too).
const PALETTE = ["#0ea5e9", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444", "#14b8a6", "#6366f1", "#ec4899"];
const STATUS_COLOR: Record<string, string> = {
  unread: "#94a3b8", in_progress: "#0ea5e9", reported: "#f59e0b", signed: "#10b981",
  completed: "#10b981", failed: "#ef4444", pending: "#94a3b8", cancelled: "#64748b",
};

type Series = { label: string; value: number }[];

function fmtMinutes(m: number | null | undefined): string {
  if (m === null || m === undefined) return "—";
  if (m < 60) return `${Math.round(m)} min`;
  if (m < 60 * 48) return `${(m / 60).toFixed(1)} h`;
  return `${(m / 1440).toFixed(1)} d`;
}

function ago(iso: string | null): string {
  if (!iso) return "—";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

function Kpi({ value, sub }: { value: number | string; sub?: React.ReactNode }) {
  return (
    <div className="flex flex-col justify-center h-full">
      <span className="text-4xl font-bold tabular-nums text-gray-900 dark:text-gray-100">{value}</span>
      {sub && <span className="text-xs text-gray-500 dark:text-gray-400 mt-1">{sub}</span>}
    </div>
  );
}

function Bars({ series, colorOf }: { series: Series; colorOf?: (label: string, i: number) => string }) {
  const max = Math.max(1, ...series.map((s) => s.value));
  if (!series.length) return <Empty />;
  return (
    <ul className="space-y-1.5">
      {series.map((s, i) => (
        <li key={s.label} className="text-xs">
          <div className="flex justify-between mb-0.5">
            <span className="text-gray-600 dark:text-gray-300 truncate">{s.label.replace(/_/g, " ")}</span>
            <span className="tabular-nums text-gray-900 dark:text-gray-100 font-medium">{s.value}</span>
          </div>
          <div className="h-1.5 rounded-full bg-gray-100 dark:bg-gray-800 overflow-hidden">
            <div
              className="h-full rounded-full"
              style={{ width: `${(s.value / max) * 100}%`, background: colorOf ? colorOf(s.label, i) : PALETTE[i % PALETTE.length] }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

function Empty({ text = "No data for this period" }: { text?: string }) {
  return <p className="text-xs text-gray-400 dark:text-gray-500 py-4 text-center">{text}</p>;
}

function StudyRows({ rows }: { rows: any[] }) {
  if (!rows?.length) return <Empty text="Nothing here right now" />;
  return (
    <ul className="divide-y divide-gray-100 dark:divide-gray-800 text-xs">
      {rows.map((r) => (
        <li key={r.study_instance_uid} className="py-1.5 flex items-center justify-between gap-2">
          <Link href={`/study/${encodeURIComponent(r.study_instance_uid)}`} className="min-w-0 hover:underline">
            <span className="font-medium text-gray-900 dark:text-gray-100 truncate block">
              {r.patient_name || r.patient_id || "Unknown patient"}
            </span>
            <span className="text-gray-500 dark:text-gray-400 truncate block">
              {[r.modality, r.description].filter(Boolean).join(" · ") || r.study_instance_uid}
            </span>
          </Link>
          <span className="shrink-0 text-right">
            <span className="block capitalize" style={{ color: STATUS_COLOR[r.reading_status] }}>
              {(r.reading_status || "").replace(/_/g, " ")}
            </span>
            <span className="block text-gray-400">{ago(r.signed_at || r.reported_at || r.received_at)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

export function WidgetBody({ type, result }: { type: string; result: WidgetData | undefined }) {
  if (!result) return <p className="text-xs text-gray-400 py-4 text-center">Loading…</p>;
  if (result.error) return <p className="text-xs text-red-500 py-4 text-center">{result.error}</p>;
  const d = result.data ?? {};

  switch (type) {
    case "kpi_studies": {
      const delta = d.delta ?? 0;
      return (
        <Kpi
          value={d.value ?? 0}
          sub={
            <span className={delta > 0 ? "text-green-600" : delta < 0 ? "text-red-500" : undefined}>
              {delta > 0 ? "+" : ""}{delta} vs previous period ({d.previous ?? 0})
            </span>
          }
        />
      );
    }
    case "todays_intake":
      return (
        <div className="grid grid-cols-2 gap-3 h-full items-center">
          <Kpi value={d.patients ?? 0} sub="patients registered" />
          <Kpi value={d.orders ?? 0} sub="orders created" />
        </div>
      );
    case "reading_status":
    case "studies_by_modality": {
      const series: Series = d.series ?? [];
      const total = series.reduce((a, s) => a + s.value, 0);
      if (!total) return <Empty />;
      return (
        <Donut
          centerLabel="studies"
          segments={series.map((s, i) => ({
            label: s.label.replace(/_/g, " "),
            value: s.value,
            color: STATUS_COLOR[s.label] ?? PALETTE[i % PALETTE.length],
          }))}
        />
      );
    }
    case "study_volume_trend": {
      const series: Series = d.series ?? [];
      if (!series.some((s) => s.value > 0)) return <Empty />;
      return <AreaTrend data={series.map((s) => ({ label: s.label.slice(5), value: s.value }))} height={150} />;
    }
    case "my_worklist":
    case "pending_signoff":
    case "overdue_studies":
    case "recent_reports":
      return <StudyRows rows={d.rows ?? []} />;
    case "critical_alerts":
      return (
        <div className="space-y-2">
          <div className="flex items-baseline gap-2">
            <span className={`text-3xl font-bold tabular-nums ${d.value ? "text-red-600" : "text-gray-900 dark:text-gray-100"}`}>
              {d.value ?? 0}
            </span>
            <span className="text-xs text-gray-500">open</span>
          </div>
          {(d.rows ?? []).length === 0 ? (
            <Empty text="No alerts" />
          ) : (
            <ul className="text-xs divide-y divide-gray-100 dark:divide-gray-800">
              {d.rows.map((a: any) => (
                <li key={a.id} className="py-1 flex justify-between gap-2">
                  <span className="truncate">{a.title}</span>
                  <span className={a.severity === "CRITICAL" ? "text-red-600" : "text-amber-600"}>{a.status}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      );
    case "turnaround_time":
      return (
        <div className="grid grid-cols-2 gap-4 h-full items-center">
          {(["report", "signoff"] as const).map((k) => (
            <div key={k}>
              <p className="text-[10px] uppercase tracking-wider text-gray-500">{k === "report" ? "To report" : "To sign-off"}</p>
              <p className="text-2xl font-bold tabular-nums text-gray-900 dark:text-gray-100">{fmtMinutes(d[k]?.median_minutes)}</p>
              <p className="text-xs text-gray-500">p90 {fmtMinutes(d[k]?.p90_minutes)} · n={d[k]?.count ?? 0}</p>
            </div>
          ))}
        </div>
      );
    case "radiologist_workload":
      if (!(d.rows ?? []).length) return <Empty />;
      return (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 text-left">
              <th className="py-1 font-medium">Radiologist</th>
              <th className="py-1 font-medium text-right">In progress</th>
              <th className="py-1 font-medium text-right">Signed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
            {d.rows.map((r: any) => (
              <tr key={r.radiologist}>
                <td className="py-1">{r.radiologist}</td>
                <td className="py-1 text-right tabular-nums">{r.in_progress}</td>
                <td className="py-1 text-right tabular-nums">{r.signed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      );
    case "ai_jobs":
      return (
        <div className="grid grid-cols-2 gap-4">
          <Bars series={d.by_status ?? []} colorOf={(l, i) => STATUS_COLOR[l] ?? PALETTE[i % PALETTE.length]} />
          <Bars series={d.series ?? []} />
        </div>
      );
    case "ai_qa_flags":
      if (!(d.rows ?? []).length) return <Empty />;
      return (
        <Bars
          series={(d.rows as any[]).map((r) => ({ label: `${r.usecase} (${r.flagged}/${r.results})`, value: r.flag_rate }))}
          colorOf={(_, i) => (i % 2 ? "#f59e0b" : "#fb923c")}
        />
      );
    case "markdown":
      return (
        <p className="text-sm whitespace-pre-wrap text-gray-700 dark:text-gray-300">
          {d.text || "Edit this widget to add a notice for your team."}
        </p>
      );
    default:
      return <Empty text={`Unknown widget: ${type}`} />;
  }
}
