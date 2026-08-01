"use client";

import { useStudies, useUsecases, useHealth, useCriticalAlertStats } from "@/lib/hooks";
import { api, UrgencyScore } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatPatientName, parseUtcDate } from "@/lib/format";
import { CountUp } from "@/components/ui/CountUp";
import { AreaTrend, Donut, type TrendPoint } from "@/components/ui/Charts";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Database, Activity, CheckCircle2, ArrowRight, Brain, Upload, UserPlus,
  ClipboardList, FileText, GitCompare, AlertTriangle, Zap, ShieldAlert,
  Server, Layers, ArrowUpRight, Stethoscope, Cpu, TrendingUp, PieChart,
  ClipboardCheck,
} from "lucide-react";

// ── helpers ──────────────────────────────────────────────────────────────────

function relativeTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const diff = Date.now() - parseUtcDate(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

function initials(name: string): string {
  const parts = name.split(" ").filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts[parts.length - 1][0] || "")).toUpperCase();
}

const MODALITY_COLOR: Record<string, string> = {
  MR: "from-cyan-500 to-teal-600",
  CT: "from-indigo-500 to-blue-600",
  PT: "from-amber-500 to-orange-600",
  NM: "from-emerald-500 to-green-600",
  US: "from-teal-500 to-cyan-600",
  MG: "from-pink-500 to-rose-600",
};
function modalityColor(m: string): string {
  return MODALITY_COLOR[m?.toUpperCase()] ?? "from-slate-500 to-slate-600";
}

const URGENCY_CHIP: Record<string, string> = {
  STAT: "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300 ring-red-200 dark:ring-red-900",
  HIGH: "bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300 ring-amber-200 dark:ring-amber-900",
};

// ── page ─────────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { data: studyData, isLoading: studiesLoading } = useStudies();
  const { data: usecases } = useUsecases();
  const { data: health } = useHealth();
  const { data: alertStats } = useCriticalAlertStats();
  const { user } = useAuth();

  const [jobStats, setJobStats] = useState({ active: 0, completed: 0, failed: 0 });
  const [urgency, setUrgency] = useState<Record<string, UrgencyScore>>({});
  const [now, setNow] = useState<Date | null>(null);
  const [trendDays, setTrendDays] = useState(14);

  useEffect(() => setNow(new Date()), []);

  // Job tallies (parallel — snappier than the previous sequential loop).
  useEffect(() => {
    const studies = studyData?.studies?.slice(0, 20) ?? [];
    if (!studies.length) return;
    let cancelled = false;
    (async () => {
      const per = await Promise.all(
        studies.map((s) =>
          api.jobs.listByStudy(s.study_instance_uid).then((d) => d.jobs).catch(() => [])
        )
      );
      if (cancelled) return;
      let active = 0, completed = 0, failed = 0;
      for (const jobs of per)
        for (const j of jobs) {
          if (j.status === "completed") completed++;
          else if (j.status === "failed") failed++;
          else if (j.status !== "cancelled") active++;
        }
      setJobStats({ active, completed, failed });
    })();
    return () => { cancelled = true; };
  }, [studyData]);

  // Urgency scores for the loaded studies.
  useEffect(() => {
    const studies = studyData?.studies ?? [];
    if (!studies.length) return;
    let cancelled = false;
    api.metrics
      .getUrgencyScores(studies.map((s) => s.study_instance_uid))
      .then((r) => {
        if (cancelled) return;
        const m: Record<string, UrgencyScore> = {};
        for (const sc of r.scores) m[sc.study_instance_uid] = sc;
        setUrgency(m);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [studyData]);

  const urgentCount = useMemo(
    () => Object.values(urgency).filter((u) => u.priority === "STAT" || u.priority === "HIGH").length,
    [urgency]
  );
  const statCount = useMemo(() => Object.values(urgency).filter((u) => u.priority === "STAT").length, [urgency]);

  const modalityMix = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const s of studyData?.studies ?? []) {
      const m = (s.modality || "OTHER").toUpperCase();
      counts[m] = (counts[m] ?? 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  }, [studyData]);
  const modalityTotal = modalityMix.reduce((a, [, n]) => a + n, 0);

  // Studies ingested per day over the last 14 days (gated on `now` to avoid an
  // SSR/client hydration mismatch from Date()).
  const trend: TrendPoint[] = useMemo(() => {
    if (!now) return [];
    const days = trendDays;
    const key = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const buckets: { k: string; label: string; value: number }[] = [];
    const index: Record<string, number> = {};
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(now);
      d.setDate(now.getDate() - i);
      const k = key(d);
      index[k] = buckets.length;
      buckets.push({ k, label: `${d.getDate()}/${d.getMonth() + 1}`, value: 0 });
    }
    for (const s of studyData?.studies ?? []) {
      if (!s.created_at) continue;
      const d = parseUtcDate(s.created_at);
      if (isNaN(d.getTime())) continue;
      const i = index[key(d)];
      if (i !== undefined) buckets[i].value++;
    }
    return buckets.map(({ label, value }) => ({ label, value }));
  }, [studyData, now, trendDays]);

  // Reading workflow breakdown (radiologist lifecycle). reading_status defaults
  // to "unread" when the backend hasn't set one yet.
  const reading = useMemo(() => {
    const c = { unread: 0, in_progress: 0, reported: 0, signed: 0 };
    for (const s of studyData?.studies ?? []) {
      const st = (s.reading_status || "unread") as keyof typeof c;
      if (st in c) c[st]++;
      else c.unread++;
    }
    return c;
  }, [studyData]);
  const readingTotal = reading.unread + reading.in_progress + reading.reported + reading.signed;
  const awaitingReading = reading.unread + reading.in_progress;

  const pipelineSegments = useMemo(
    () => [
      { label: "Completed", value: jobStats.completed, color: "#10b981" },
      { label: "In progress", value: jobStats.active, color: "#f59e0b" },
      { label: "Failed", value: jobStats.failed, color: "#f43f5e" },
    ],
    [jobStats]
  );
  const pipelineTotal = jobStats.completed + jobStats.active + jobStats.failed;
  const trendTotal = trend.reduce((a, t) => a + t.value, 0);

  const enabledModels = usecases?.filter((u) => u.enabled).length ?? 0;
  const healthy = health?.status === "ok";
  const pendingCritical = alertStats?.pending_critical ?? 0;

  const hour = now?.getHours() ?? 9;
  const greeting = !now
    ? "Welcome back"
    : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const dateStr = now
    ? now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })
    : "";
  const displayName = user?.username
    ? user.username.charAt(0).toUpperCase() + user.username.slice(1)
    : "Clinician";

  const kpis = [
    { label: "Total Studies", value: studyData?.total ?? 0, icon: Database, grad: "from-cyan-500 to-teal-600",
      sub: `${studyData?.studies?.length ?? 0} loaded`, href: "/worklist" },
    { label: "Urgent", value: urgentCount, icon: Zap, grad: statCount ? "from-red-500 to-rose-600" : "from-amber-500 to-orange-600",
      sub: statCount ? `${statCount} STAT` : "priority review", href: "/worklist" },
    { label: "Active Pipelines", value: jobStats.active, icon: Activity, grad: "from-violet-500 to-indigo-600",
      sub: jobStats.active ? "processing now" : "queue clear", href: "/worklist" },
    { label: "Completed", value: jobStats.completed, icon: CheckCircle2, grad: "from-emerald-500 to-green-600",
      sub: "AI analyses", href: "/reports" },
  ];

  const readingItems = [
    { label: "Unread", count: reading.unread, color: "bg-slate-400" },
    { label: "Reading", count: reading.in_progress, color: "bg-blue-500" },
    { label: "Reported", count: reading.reported, color: "bg-violet-500" },
    { label: "Signed", count: reading.signed, color: "bg-emerald-500" },
  ];

  const quickActions = [
    { label: "Upload DICOM", desc: "Ingest from PACS", icon: Upload, href: "/upload" },
    { label: "Patient Intake", desc: "Record clinical data", icon: UserPlus, href: "/onboarding" },
    { label: "Worklist", desc: "Studies & AI status", icon: ClipboardList, href: "/worklist" },
    { label: "Reports", desc: "Signed & drafts", icon: FileText, href: "/reports" },
    { label: "Compare", desc: "Prior vs current", icon: GitCompare, href: "/compare" },
  ];

  return (
    <div className="space-y-6">
      {/* ── Hero ─────────────────────────────────────────────────────────── */}
      <div className="relative overflow-hidden glass-raised rounded-3xl accent-top px-6 py-6 sm:px-8 sm:py-7">
        <div className="pointer-events-none absolute -top-24 -right-16 w-80 h-80 rounded-full bg-accent/15 blur-3xl float" />
        <div className="pointer-events-none absolute -bottom-28 right-1/3 w-72 h-72 rounded-full bg-accent-2/10 blur-3xl" />
        {/* subtle accent dot-grid for depth, fading out to the right */}
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.12] dark:opacity-[0.18]"
          style={{
            backgroundImage: "radial-gradient(circle at 1px 1px, rgb(var(--accent)) 1px, transparent 0)",
            backgroundSize: "24px 24px",
            WebkitMaskImage: "linear-gradient(105deg, black, transparent 62%)",
            maskImage: "linear-gradient(105deg, black, transparent 62%)",
          }}
        />
        <div className="relative flex flex-col lg:flex-row lg:items-center lg:justify-between gap-5">
          <div>
            <p className="text-xs font-mono uppercase tracking-[0.2em] text-accent mb-1">{dateStr || " "}</p>
            <h1 className="text-3xl sm:text-4xl font-bold tracking-tight">
              {greeting}, <span className="text-gradient-anim">{displayName}</span>
            </h1>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="inline-flex items-center gap-1.5">
                <span className={`w-2 h-2 rounded-full ${healthy ? "bg-emerald-500 animate-pulse" : "bg-red-500"}`} />
                {healthy ? "All systems operational" : "System degraded"}
              </span>
              <span className="inline-flex items-center gap-1.5">
                <Cpu className="w-3.5 h-3.5" /> {enabledModels} AI models online
              </span>
              {health?.version && (
                <span className="inline-flex items-center gap-1.5 text-gray-400 dark:text-gray-500">
                  <Server className="w-3.5 h-3.5" /> v{health.version}
                </span>
              )}
              {awaitingReading > 0 && (
                <Link href="/worklist" className="inline-flex items-center gap-1.5 font-medium text-primary-600 dark:text-primary-400 hover:text-primary-500 transition-colors">
                  <ClipboardCheck className="w-3.5 h-3.5" /> {awaitingReading} awaiting reading
                </Link>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2.5 shrink-0">
            <Link href="/upload" className="btn-gradient flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-xl">
              <Upload className="w-4 h-4" /> Upload DICOM
            </Link>
            <Link
              href="/onboarding"
              className="press flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-xl border border-border bg-surface/40 hover:bg-surface/70 dark:hover:bg-white/5 text-gray-700 dark:text-gray-200 transition-colors"
            >
              <UserPlus className="w-4 h-4" /> New Patient
            </Link>
          </div>
        </div>
      </div>

      {/* ── Critical alert strip (only when there's something urgent) ─────── */}
      {pendingCritical > 0 && (
        <Link
          href="/admin/alerts"
          className="group flex items-center gap-3 rounded-2xl px-5 py-3.5 border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 hover-lift"
        >
          <span className="grid place-items-center w-10 h-10 rounded-xl bg-red-500/15 text-red-600 dark:text-red-400 shrink-0">
            <ShieldAlert className="w-5 h-5" />
          </span>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-red-800 dark:text-red-300">
              {pendingCritical} critical finding{pendingCritical > 1 ? "s" : ""} need acknowledgement
            </p>
            <p className="text-xs text-red-600 dark:text-red-400">Review and acknowledge in the alerts centre</p>
          </div>
          <ArrowRight className="w-4 h-4 text-red-500 transition-transform group-hover:translate-x-1" />
        </Link>
      )}

      {/* ── KPI row ──────────────────────────────────────────────────────── */}
      <div className="stagger grid grid-cols-2 lg:grid-cols-4 gap-4">
        {kpis.map((k) => {
          const Icon = k.icon;
          return (
            <Link key={k.label} href={k.href} className="group glass accent-top rounded-2xl p-5 hover-lift">
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">{k.label}</p>
                  <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1.5 tabular-nums">
                    <CountUp value={k.value} />
                  </p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{k.sub}</p>
                </div>
                <div className={`grid place-items-center w-12 h-12 rounded-xl shadow-glow-sm text-white bg-gradient-to-br ${k.grad} transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-3`}>
                  <Icon className="w-6 h-6" />
                </div>
              </div>
            </Link>
          );
        })}
      </div>

      {/* ── Visuals row: activity trend + pipeline status ────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 glass rounded-2xl p-5">
          <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-accent" />
              <h2 className="font-semibold text-gray-900 dark:text-gray-100">Study Activity</h2>
              <span className="text-xs text-gray-400 dark:text-gray-500 ms-1">
                <span className="font-semibold text-gray-700 dark:text-gray-300 tabular-nums">{trendTotal}</span> ingested
              </span>
            </div>
            {/* Time-range segmented control */}
            <div className="flex items-center gap-0.5 p-0.5 rounded-lg border border-border bg-gray-50 dark:bg-white/5 text-xs shrink-0">
              {[7, 14, 30].map((d) => (
                <button
                  key={d}
                  onClick={() => setTrendDays(d)}
                  className={`press px-2.5 py-1 rounded-md font-medium transition-all ${
                    trendDays === d
                      ? "bg-primary-600 text-white shadow-glow-sm"
                      : "text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
                  }`}
                >
                  {d}d
                </button>
              ))}
            </div>
          </div>
          {trend.length ? (
            <AreaTrend data={trend} />
          ) : (
            <div className="h-[180px] rounded-xl bg-gray-100/50 dark:bg-white/5 shimmer" />
          )}
        </div>

        <div className="glass rounded-2xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <PieChart className="w-4 h-4 text-accent" />
            <h2 className="font-semibold text-gray-900 dark:text-gray-100">Pipeline Status</h2>
          </div>
          {pipelineTotal > 0 ? (
            <Donut segments={pipelineSegments} centerLabel="Jobs" />
          ) : (
            <div className="py-8 text-center text-sm text-gray-400 dark:text-gray-500">
              No pipeline runs yet
            </div>
          )}
        </div>
      </div>

      {/* ── Main grid ────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Recent Studies (wide) */}
        <div className="lg:col-span-2 glass rounded-2xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-border">
            <div className="flex items-center gap-2">
              <Stethoscope className="w-4 h-4 text-accent" />
              <h2 className="font-semibold text-gray-900 dark:text-gray-100">Recent Studies</h2>
            </div>
            <Link href="/worklist" className="group text-sm text-primary-600 dark:text-primary-400 hover:text-primary-500 flex items-center gap-1">
              View all <ArrowRight className="w-3.5 h-3.5 transition-transform duration-200 group-hover:translate-x-1" />
            </Link>
          </div>
          <div className="divide-y divide-border">
            {studiesLoading && !studyData ? (
              [...Array(5)].map((_, i) => (
                <div key={i} className="flex items-center gap-3 px-5 py-3.5">
                  <div className="w-10 h-10 rounded-xl bg-gray-100 dark:bg-white/5 shimmer" />
                  <div className="flex-1 space-y-2">
                    <div className="h-3 w-40 rounded bg-gray-100 dark:bg-white/5 shimmer" />
                    <div className="h-2.5 w-24 rounded bg-gray-100 dark:bg-white/5 shimmer" />
                  </div>
                </div>
              ))
            ) : studyData?.studies?.length ? (
              studyData.studies.slice(0, 7).map((s) => {
                const u = urgency[s.study_instance_uid];
                const name = formatPatientName(s.patient_name);
                const mod = (s.modality || "").toUpperCase();
                return (
                  <Link
                    key={s.study_instance_uid}
                    href={`/study/${s.study_instance_uid}`}
                    className="group flex items-center gap-3 px-5 py-3.5 hover:bg-accent/5 transition-colors"
                  >
                    <span className={`grid place-items-center w-10 h-10 rounded-xl text-white text-xs font-bold shrink-0 bg-gradient-to-br ${modalityColor(mod)}`}>
                      {mod ? mod.slice(0, 2) : initials(name)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate transition-colors group-hover:text-primary-600 dark:group-hover:text-primary-400">
                        {name}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                        {s.study_description || s.body_part_examined || "Study"}
                        <span className="text-gray-400 dark:text-gray-500"> · {relativeTime(s.created_at)}</span>
                      </p>
                    </div>
                    {s.series?.length > 0 && (
                      <span className="hidden sm:inline-flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-white/5 px-2 py-0.5 rounded-full shrink-0">
                        <Layers className="w-3 h-3" /> {s.series.length}
                      </span>
                    )}
                    {u && (u.priority === "STAT" || u.priority === "HIGH") && (
                      <span className={`inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full ring-1 shrink-0 ${URGENCY_CHIP[u.priority]}`}>
                        {u.priority === "STAT" && <AlertTriangle className="w-2.5 h-2.5" />}
                        {u.priority}
                      </span>
                    )}
                    <ArrowRight className="w-4 h-4 text-gray-400 dark:text-gray-500 transition-all duration-200 group-hover:translate-x-1 group-hover:text-primary-500 shrink-0" />
                  </Link>
                );
              })
            ) : (
              <div className="px-5 py-12 text-center">
                <p className="text-sm text-gray-500 dark:text-gray-400">No studies yet</p>
                <Link href="/upload" className="inline-flex items-center gap-1.5 mt-3 text-sm text-primary-600 dark:text-primary-400 hover:underline">
                  <Upload className="w-4 h-4" /> Ingest your first study
                </Link>
              </div>
            )}
          </div>
        </div>

        {/* Right column */}
        <div className="space-y-6">
          {/* System status */}
          <div className="glass rounded-2xl overflow-hidden">
            <div className="px-5 py-4 border-b border-border flex items-center gap-2">
              <Server className="w-4 h-4 text-accent" />
              <h2 className="font-semibold text-gray-900 dark:text-gray-100">System Status</h2>
            </div>
            <div className="p-3 space-y-1">
              {[
                { label: "Backend API", ok: healthy, detail: health?.version ? `v${health.version}` : "—" },
                { label: "AI Models", ok: enabledModels > 0, detail: `${enabledModels} enabled` },
                { label: "Critical Alerts", ok: pendingCritical === 0, detail: pendingCritical === 0 ? "clear" : `${pendingCritical} pending`, warn: pendingCritical > 0 },
              ].map((r) => (
                <div key={r.label} className="flex items-center justify-between px-2 py-2 rounded-lg hover:bg-accent/5 transition-colors">
                  <div className="flex items-center gap-2.5">
                    <span className={`w-2 h-2 rounded-full ${r.warn ? "bg-red-500" : r.ok ? "bg-emerald-500" : "bg-gray-400"}`} />
                    <span className="text-sm text-gray-700 dark:text-gray-300">{r.label}</span>
                  </div>
                  <span className={`text-xs font-medium ${r.warn ? "text-red-600 dark:text-red-400" : "text-gray-500 dark:text-gray-400"}`}>{r.detail}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Reading workflow */}
          <div className="glass rounded-2xl overflow-hidden">
            <div className="flex items-center justify-between px-5 py-4 border-b border-border">
              <div className="flex items-center gap-2">
                <ClipboardCheck className="w-4 h-4 text-accent" />
                <h2 className="font-semibold text-gray-900 dark:text-gray-100">Reading Workflow</h2>
              </div>
              <Link href="/worklist" className="group text-sm text-primary-600 dark:text-primary-400 hover:text-primary-500 flex items-center gap-1">
                Open <ArrowRight className="w-3.5 h-3.5 transition-transform duration-200 group-hover:translate-x-1" />
              </Link>
            </div>
            <div className="p-5 space-y-4">
              {readingTotal > 0 ? (
                <>
                  {/* Stacked lifecycle bar */}
                  <div className="flex h-2.5 rounded-full overflow-hidden gap-0.5 bg-gray-100 dark:bg-white/5">
                    {readingItems.map((r) =>
                      r.count > 0 ? (
                        <div
                          key={r.label}
                          className={`${r.color} transition-all duration-700`}
                          style={{ width: `${(r.count / readingTotal) * 100}%` }}
                          title={`${r.label}: ${r.count}`}
                        />
                      ) : null
                    )}
                  </div>
                  {/* Legend */}
                  <div className="grid grid-cols-2 gap-x-4 gap-y-2.5">
                    {readingItems.map((r) => (
                      <div key={r.label} className="flex items-center gap-2 text-sm">
                        <span className={`w-2.5 h-2.5 rounded-sm shrink-0 ${r.color}`} />
                        <span className="text-gray-600 dark:text-gray-300 flex-1 truncate">{r.label}</span>
                        <span className="font-semibold text-gray-900 dark:text-gray-100 tabular-nums">{r.count}</span>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="text-sm text-gray-400 dark:text-gray-500">No studies in the reading queue</p>
              )}
            </div>
          </div>

          {/* Modality mix */}
          <div className="glass rounded-2xl overflow-hidden">
            <div className="px-5 py-4 border-b border-border flex items-center gap-2">
              <Layers className="w-4 h-4 text-accent" />
              <h2 className="font-semibold text-gray-900 dark:text-gray-100">Modality Mix</h2>
            </div>
            <div className="p-5 space-y-3">
              {modalityMix.length ? (
                modalityMix.map(([m, n]) => {
                  const pct = modalityTotal ? Math.round((n / modalityTotal) * 100) : 0;
                  return (
                    <div key={m}>
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="font-medium text-gray-700 dark:text-gray-300">{m}</span>
                        <span className="text-gray-400 dark:text-gray-500 tabular-nums">{n} · {pct}%</span>
                      </div>
                      <div className="h-2 rounded-full bg-gray-100 dark:bg-white/5 overflow-hidden">
                        <div className={`h-full rounded-full bg-gradient-to-r ${modalityColor(m)} transition-all duration-700`} style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  );
                })
              ) : (
                <p className="text-sm text-gray-400 dark:text-gray-500">No data yet</p>
              )}
            </div>
          </div>

          {/* AI models compact */}
          <div className="glass rounded-2xl overflow-hidden">
            <div className="flex items-center justify-between px-5 py-4 border-b border-border">
              <div className="flex items-center gap-2">
                <Brain className="w-4 h-4 text-accent" />
                <h2 className="font-semibold text-gray-900 dark:text-gray-100">AI Models</h2>
              </div>
              <Link href="/admin/usecases" className="group text-sm text-primary-600 dark:text-primary-400 hover:text-primary-500 flex items-center gap-1">
                Manage <ArrowRight className="w-3.5 h-3.5 transition-transform duration-200 group-hover:translate-x-1" />
              </Link>
            </div>
            <div className="p-3 max-h-64 overflow-y-auto space-y-0.5">
              {usecases?.length ? (
                usecases.map((uc) => (
                  <div key={uc.name} className="flex items-center justify-between px-2 py-2 rounded-lg hover:bg-accent/5 transition-colors">
                    <span className="text-sm text-gray-700 dark:text-gray-300 truncate">
                      {uc.name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                    </span>
                    <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full shrink-0 ${
                      uc.enabled
                        ? "bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300"
                        : "bg-gray-100 dark:bg-white/5 text-gray-500 dark:text-gray-400"
                    }`}>
                      {uc.enabled ? "ON" : "OFF"}
                    </span>
                  </div>
                ))
              ) : (
                <p className="px-2 py-3 text-sm text-gray-400 dark:text-gray-500">No models registered</p>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── Quick actions ────────────────────────────────────────────────── */}
      <div>
        <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-3">Quick actions</h2>
        <div className="stagger grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          {quickActions.map((a) => {
            const Icon = a.icon;
            return (
              <Link key={a.label} href={a.href} className="group glass rounded-2xl p-4 hover-lift">
                <span className="grid place-items-center w-11 h-11 rounded-xl bg-accent/10 ring-1 ring-accent/20 text-accent mb-3 transition-transform duration-300 group-hover:scale-110">
                  <Icon className="w-5 h-5" />
                </span>
                <p className="text-sm font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-1">
                  {a.label}
                  <ArrowUpRight className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 transition-all duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 group-hover:text-accent" />
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{a.desc}</p>
              </Link>
            );
          })}
        </div>
      </div>
    </div>
  );
}
