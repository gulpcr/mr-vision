"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, Study, Job, UseCase, UrgencyScore } from "@/lib/api";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { EmptyState } from "@/components/ui/EmptyState";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { Table, Caption, Th, SortableTh } from "@/components/ui/Table";
import { CountUp } from "@/components/ui/CountUp";
import { formatDate, formatPatientName, parseUtcDate } from "@/lib/format";
import { useLocale } from "@/lib/i18n";
import Link from "next/link";
import {
  Search, Upload, AlertTriangle, Zap,
  TrendingUp, RefreshCw, Clock, Layers, X, PlayCircle,
  ClipboardList, ChevronRight, ChevronLeft, Trash2, RotateCcw, Square,
  CheckCircle2,
} from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────────────

type SortField = "patient_name" | "study_date" | "body_part_examined" | "urgency" | "last_run";
type SortDir = "asc" | "desc";
type DateFilter = "today" | "week" | "all";
type AIStatusFilter = "any" | "not_started" | "in_progress" | "completed" | "failed";

const PRIORITY_CONFIG = {
  STAT:    { label: "STAT",    bg: "bg-red-100 dark:bg-red-900",    text: "text-red-700 dark:text-red-300",    border: "border-red-200 dark:border-red-800",    order: 0 },
  HIGH:    { label: "HIGH",    bg: "bg-orange-100 dark:bg-orange-900", text: "text-orange-700 dark:text-orange-300", border: "border-orange-200 dark:border-orange-800", order: 1 },
  NORMAL:  { label: "NORMAL",  bg: "bg-blue-50 dark:bg-blue-950",    text: "text-blue-600 dark:text-blue-400",   border: "border-blue-100 dark:border-blue-900",   order: 2 },
  ROUTINE: { label: "ROUTINE", bg: "bg-gray-50 dark:bg-gray-800",    text: "text-gray-500 dark:text-gray-400",   border: "border-gray-100 dark:border-gray-800",   order: 3 },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

// UTC-safe ms-since-epoch for a raw backend timestamp (see parseUtcDate).
function toUtcMs(dateStr: string | null | undefined): number {
  if (!dateStr) return 0;
  return parseUtcDate(dateStr).getTime();
}

function relativeTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const diff = Date.now() - toUtcMs(dateStr);
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(dateStr);
}

const ACTIVE_STATUSES = ["pending", "routing", "preprocessing", "inferring", "postprocessing"];
const STALE_MS = 15 * 60 * 1000; // 15 minutes without an update → stale

function latestPerUsecase(studyJobs: Job[]): Job[] {
  // studyJobs is already sorted newest-first by the backend (created_at DESC)
  const seen: Record<string, Job> = {};
  for (const j of studyJobs) {
    if (!seen[j.usecase_name]) seen[j.usecase_name] = j;
  }
  return Object.values(seen);
}

// AI status is derived from the LATEST job per use case only, not all historical jobs.
function studyAIStatus(studyJobs: Job[]): AIStatusFilter {
  const latest = latestPerUsecase(studyJobs);
  if (latest.length === 0) return "not_started";
  if (latest.some((j) => ACTIVE_STATUSES.includes(j.status))) return "in_progress";
  if (latest.some((j) => j.status === "completed")) return "completed";
  if (latest.every((j) => j.status === "failed" || j.status === "cancelled")) return "failed";
  return "not_started";
}

function isStale(job: Job): boolean {
  if (!ACTIVE_STATUSES.includes(job.status)) return false;
  return Date.now() - toUtcMs(job.updated_at) > STALE_MS;
}

// Compact page list with ellipses: always shows first, last, and the current
// page ± 1 (e.g. [1, "…", 5, 6, 7, "…", 20]). Small ranges (≤ 7) show every page.
function pageRange(current: number, total: number): (number | "…")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const wanted = new Set<number>([1, total, current, current - 1, current + 1]);
  const pages = Array.from(wanted).filter((p) => p >= 1 && p <= total).sort((a, b) => a - b);
  const out: (number | "…")[] = [];
  let prev = 0;
  for (const p of pages) {
    if (p - prev > 1) out.push("…");
    out.push(p);
    prev = p;
  }
  return out;
}

// Most recent job activity for a study (max created_at across its jobs), 0 if it
// has never been run. Drives the "Last Run" column + default sort.
function lastRunMs(studyJobs: Job[] | undefined): number {
  if (!studyJobs?.length) return 0;
  let m = 0;
  for (const j of studyJobs) {
    const t = toUtcMs(j.created_at);
    if (t > m) m = t;
  }
  return m;
}

// ── Stat Card ────────────────────────────────────────────────────────────────

function StatCard({
  label, value, sub, color = "gray",
}: {
  label: string; value: number | string; sub?: string;
  color?: "gray" | "red" | "amber" | "green";
}) {
  const val = {
    gray: "text-gray-900 dark:text-gray-100",
    red: "text-red-600 dark:text-red-400",
    amber: "text-amber-600 dark:text-amber-400",
    green: "text-emerald-600 dark:text-emerald-400",
  }[color];
  return (
    <div className="glass accent-top rounded-2xl px-4 py-3.5 hover-lift">
      <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">{label}</p>
      <p className={`text-2xl font-bold tabular-nums ${val}`}><CountUp value={value} /></p>
      {sub && <p className="text-xs mt-0.5 text-gray-500 dark:text-gray-400">{sub}</p>}
    </div>
  );
}

// ── Use-case Modal ───────────────────────────────────────────────────────────

function UsecaseModal({
  studyUid, usecases, running, onClose, onSelect,
}: {
  studyUid: string; usecases: UseCase[]; running: boolean;
  onClose: () => void;
  onSelect: (uid: string, names?: string[]) => void;
}) {
  return (
    <Modal open title="Run AI Pipeline" onClose={onClose} size="sm">
      <button
        onClick={() => onSelect(studyUid)}
        disabled={running}
        className="w-full flex items-center gap-2 px-3 py-2.5 text-sm font-medium text-left rounded text-primary-700 bg-primary-50 hover:bg-primary-100 disabled:opacity-50 transition-colors mb-1"
      >
        <Zap className="w-4 h-4 shrink-0" />
        <div>
          <div>Auto-Route</div>
          <div className="text-xs font-normal text-primary-500">Let the system pick the best pipeline</div>
        </div>
      </button>
      {usecases.filter((uc) => uc.enabled).length > 0 && (
        <>
          <div className="text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-500 px-3 py-1.5">
            Or choose a specific pipeline
          </div>
          {usecases.filter((uc) => uc.enabled).map((uc) => (
            <button
              key={uc.name}
              onClick={() => onSelect(studyUid, [uc.name])}
              disabled={running}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left rounded text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 dark:hover:bg-surface-raised disabled:opacity-50 transition-colors"
            >
              <ChevronRight className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0" />
              {uc.name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
            </button>
          ))}
        </>
      )}
    </Modal>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function WorklistPage() {
  const router = useRouter();
  const { strings } = useLocale();

  const [studies, setStudies]       = useState<Study[]>([]);
  const [jobs, setJobs]             = useState<Record<string, Job[]>>({});
  const [usecases, setUsecases]     = useState<UseCase[]>([]);
  const [urgencyMap, setUrgencyMap] = useState<Record<string, UrgencyScore>>({});
  const [total, setTotal]           = useState(0);
  const [loading, setLoading]       = useState(true);
  const [urgencyLoading, setUrgencyLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed]   = useState<Date | null>(null);
  const [runningAI, setRunningAI]       = useState<string | null>(null);
  const [modalStudyUid, setModalStudyUid] = useState<string | null>(null);
  const [jobError, setJobError]         = useState<string | null>(null);
  const [runSuccess, setRunSuccess]     = useState<string | null>(null); // usecase name
  const [deletingStudy, setDeletingStudy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [cancellingJob, setCancellingJob] = useState<string | null>(null);

  // Filters
  const [search, setSearch]                   = useState("");
  const [bodyPartFilter, setBodyPartFilter]   = useState("");
  const [modalityFilter, setModalityFilter]   = useState("");
  const [referrerFilter, setReferrerFilter]   = useState("");
  const [priorityFilter, setPriorityFilter]   = useState("");
  const [dateFilter, setDateFilter]           = useState<DateFilter>("all");
  const [aiStatusFilter, setAIStatusFilter]   = useState<AIStatusFilter>("any");
  const [sortField, setSortField]             = useState<SortField>("last_run");
  const [sortDir, setSortDir]                 = useState<SortDir>("desc");

  // Pagination
  const [page, setPage]         = useState(1);
  const [pageSize, setPageSize] = useState(10);

  const searchRef = useRef<HTMLInputElement>(null);

  // ── Data loading ──────────────────────────────────────────────────────────

  const loadStudies = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const data = await api.studies.list();
      setStudies(data.studies);
      setTotal(data.total);

      // Parallel job fetching (was sequential — N+1 requests)
      const jobEntries = await Promise.all(
        data.studies.map(async (study) => {
          try {
            const jdata = await api.jobs.listByStudy(study.study_instance_uid);
            return [study.study_instance_uid, jdata.jobs] as const;
          } catch {
            return [study.study_instance_uid, [] as Job[]] as const;
          }
        })
      );
      setJobs(Object.fromEntries(jobEntries));

      if (data.studies.length > 0) {
        setUrgencyLoading(true);
        try {
          const uids = data.studies.map((s) => s.study_instance_uid);
          const result = await api.metrics.getUrgencyScores(uids);
          const map: Record<string, UrgencyScore> = {};
          for (const score of result.scores) map[score.study_instance_uid] = score;
          setUrgencyMap(map);
        } catch {
          // non-critical
        } finally {
          setUrgencyLoading(false);
        }
      }
      setLastRefreshed(new Date());
    } catch (e) {
      console.error("Failed to load studies:", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadStudies();
    api.usecases.list().then((d) => setUsecases(d.usecases)).catch(() => {});
    const interval = setInterval(() => loadStudies(), 30000);
    return () => clearInterval(interval);
  }, [loadStudies]);

  // Keyboard shortcut: / or Ctrl+K → focus search
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if ((e.key === "/" || (e.ctrlKey && e.key === "k")) && !["INPUT", "TEXTAREA"].includes(tag)) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Any change to filters, sort, or page size returns to the first page.
  useEffect(() => {
    setPage(1);
  }, [search, bodyPartFilter, modalityFilter, referrerFilter, priorityFilter, dateFilter, aiStatusFilter, sortField, sortDir, pageSize]);

  // ── Actions ───────────────────────────────────────────────────────────────

  const handleRunAI = async (studyUid: string, usecaseNames?: string[]) => {
    setModalStudyUid(null);
    setJobError(null);
    setRunSuccess(null);
    setRunningAI(studyUid);
    try {
      const data = await api.jobs.create(studyUid, usecaseNames);
      const label = data.jobs.map((j) => j.usecase_name.replace(/_/g, " ")).join(", ");
      setRunSuccess(label);
      setTimeout(() => setRunSuccess(null), 4000);
      await loadStudies();
    } catch (e: any) {
      const msg = e.message || "Unknown error";
      setJobError(msg);
      console.error("Job creation failed:", msg);
    } finally {
      setRunningAI(null);
    }
  };

  const handleCancelJob = async (jobId: string) => {
    setCancellingJob(jobId);
    setJobError(null);
    try {
      await api.jobs.cancel(jobId);
      await loadStudies();
    } catch (e: any) {
      setJobError(e.message || "Failed to stop job");
    } finally {
      setCancellingJob(null);
    }
  };

  const handleDeleteStudy = async (studyUid: string) => {
    setDeletingStudy(studyUid);
    setConfirmDelete(null);
    try {
      await api.studies.delete(studyUid);
      await loadStudies();
    } catch (e: any) {
      setJobError(e.message || "Delete failed");
    } finally {
      setDeletingStudy(null);
    }
  };

  const toggleSort = (field: SortField) => {
    if (sortField === field) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortField(field); setSortDir("asc"); }
  };

  const clearFilters = () => {
    setSearch(""); setBodyPartFilter(""); setModalityFilter("");
    setReferrerFilter("");
    setPriorityFilter(""); setDateFilter("all"); setAIStatusFilter("any");
  };

  // ── Derived data ──────────────────────────────────────────────────────────

  const bodyParts = Array.from(new Set(studies.map((s) => s.body_part_examined).filter(Boolean)));
  const modalities = Array.from(new Set(studies.map((s) => s.modality).filter(Boolean)));
  const referrers = Array.from(new Set(studies.map((s) => s.referring_physician).filter(Boolean)));

  const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);
  const weekStart  = new Date(todayStart); weekStart.setDate(weekStart.getDate() - 7);

  const filtered = studies
    .filter((s) => {
      if (search) {
        const q = search.toLowerCase();
        if (
          !formatPatientName(s.patient_name).toLowerCase().includes(q) &&
          !s.patient_id?.toLowerCase().includes(q) &&
          !s.study_description?.toLowerCase().includes(q) &&
          !s.accession_number?.toLowerCase().includes(q)
        ) return false;
      }
      if (bodyPartFilter && s.body_part_examined !== bodyPartFilter) return false;
      if (modalityFilter && s.modality !== modalityFilter) return false;
      if (referrerFilter && s.referring_physician !== referrerFilter) return false;
      if (priorityFilter && urgencyMap[s.study_instance_uid]?.priority !== priorityFilter) return false;
      // Today / 7 Days filter on LAST RUN (most recent job), not the acquisition
      // date. Studies never run are excluded from the recent-run windows.
      if (dateFilter !== "all") {
        const lr = lastRunMs(jobs[s.study_instance_uid]);
        if (lr === 0) return false;
        if (dateFilter === "today" && lr < todayStart.getTime()) return false;
        if (dateFilter === "week"  && lr < weekStart.getTime())  return false;
      }
      if (aiStatusFilter !== "any") {
        if (studyAIStatus(jobs[s.study_instance_uid] || []) !== aiStatusFilter) return false;
      }
      return true;
    })
    .sort((a, b) => {
      const dir = sortDir === "asc" ? 1 : -1;
      if (sortField === "last_run") {
        // Never-run studies (0) sink to the bottom regardless of direction.
        const ma = lastRunMs(jobs[a.study_instance_uid]);
        const mb = lastRunMs(jobs[b.study_instance_uid]);
        if (ma === 0 || mb === 0) return mb - ma; // runs before never-run
        return (ma - mb) * dir;
      }
      if (sortField === "urgency") {
        const pa = PRIORITY_CONFIG[urgencyMap[a.study_instance_uid]?.priority ?? "ROUTINE"].order;
        const pb = PRIORITY_CONFIG[urgencyMap[b.study_instance_uid]?.priority ?? "ROUTINE"].order;
        if (pa !== pb) return (pa - pb) * dir;
        return ((urgencyMap[b.study_instance_uid]?.score ?? 0) - (urgencyMap[a.study_instance_uid]?.score ?? 0)) * dir;
      }
      const va = (a[sortField as keyof Study] ?? "") as string;
      const vb = (b[sortField as keyof Study] ?? "") as string;
      return va.localeCompare(vb) * dir;
    });

  // Stat card counts
  const statCount   = Object.values(urgencyMap).filter((u) => u.priority === "STAT").length;
  const highCount   = Object.values(urgencyMap).filter((u) => u.priority === "HIGH").length;
  // Count only the LATEST job per use case per study — historical pending/failed jobs are excluded.
  const pendingJobs = Object.values(jobs)
    .flatMap(latestPerUsecase)
    .filter((j) => ACTIVE_STATUSES.includes(j.status) && !isStale(j))
    .length;
  const completedToday = Object.values(jobs).flat().filter((j) =>
    j.status === "completed" && j.completed_at && toUtcMs(j.completed_at) >= todayStart.getTime()
  ).length;

  const activeFilters = [
    search, bodyPartFilter, modalityFilter, referrerFilter, priorityFilter,
    dateFilter !== "all" ? "1" : "", aiStatusFilter !== "any" ? "1" : "",
  ].filter(Boolean).length;

  // ── Pagination (derived; currentPage is clamped so shrinking the result set
  // never leaves you stranded on an empty page) ──
  const totalPages  = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paged       = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const startIdx    = filtered.length === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const endIdx      = Math.min(currentPage * pageSize, filtered.length);

  return (
    <div>
      {/* Header ──────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-gradient">{strings.worklist.title}</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
            {total} stud{total === 1 ? "y" : "ies"}
            {lastRefreshed && (
              <span className="ms-2 text-gray-400 dark:text-gray-500 dark:text-gray-500 dark:text-gray-400 dark:text-gray-500">
                · Updated {relativeTime(lastRefreshed.toISOString())}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => loadStudies(true)}
            disabled={refreshing}
            aria-label="Refresh worklist"
            title="Refresh worklist"
            className="press p-2.5 text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 border border-border hover:bg-accent/5 rounded-xl transition-colors disabled:opacity-40"
          >
            <RefreshCw className={`w-4 h-4 ${refreshing ? "animate-spin motion-reduce:animate-none" : ""}`} />
          </button>
          <Link
            href="/upload"
            className="btn-gradient flex items-center gap-2 px-4 py-2 text-sm font-semibold rounded-xl"
          >
            <Upload className="w-4 h-4" /> Upload DICOM
          </Link>
        </div>
      </div>

      {/* Stat Cards ──────────────────────────────────────────────────────── */}
      <div className="stagger grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
        <StatCard label="Total Studies" value={total} color="gray" />
        <StatCard
          label="Urgent"
          value={statCount + highCount}
          sub={
            statCount > 0 && highCount > 0 ? `${statCount} STAT · ${highCount} HIGH`
            : statCount > 0 ? `${statCount} STAT`
            : highCount > 0 ? `${highCount} HIGH`
            : "None pending"
          }
          color={statCount > 0 ? "red" : highCount > 0 ? "amber" : "gray"}
        />
        <StatCard
          label="AI In Progress"
          value={pendingJobs}
          sub={pendingJobs > 0 ? "active pipelines" : "Queue clear"}
          color={pendingJobs > 0 ? "amber" : "gray"}
        />
        <StatCard
          label="Completed Today"
          value={completedToday}
          sub={completedToday > 0 ? "since midnight" : "None yet today"}
          color={completedToday > 0 ? "green" : "gray"}
        />
      </div>

      {/* Run AI success banner */}
      {runSuccess && (
        <div className="flex items-center gap-3 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-xl px-4 py-2.5 mb-2 animate-fade-up">
          <CheckCircle2 className="w-4 h-4 text-green-500 dark:text-green-400 shrink-0" />
          <p className="text-sm text-green-800 dark:text-green-300 flex-1">
            <span className="font-semibold">Pipeline queued:</span> {runSuccess} — the worker will start processing shortly.
          </p>
          <button
            onClick={() => setRunSuccess(null)}
            aria-label="Dismiss"
            className="text-green-400 hover:text-green-600 dark:hover:text-green-400 dark:hover:text-green-300"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Job dispatch error banner */}
      {jobError && (
        <ErrorBanner
          title="Failed to start AI pipeline"
          message={jobError}
          onDismiss={() => setJobError(null)}
          className="mb-2"
        />
      )}

      {/* Filters ─────────────────────────────────────────────────────────── */}
      <div className="glass rounded-2xl mb-4">
        <div className="flex flex-wrap items-center gap-2.5 px-3.5 py-3">
          {/* Search */}
          <div className="relative flex-1 min-w-[240px]">
            <Search className="w-4 h-4 absolute start-3 top-1/2 -translate-y-1/2 text-gray-400 dark:text-gray-500" />
            <input
              ref={searchRef}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={strings.worklist.searchPlaceholder}
              className="w-full ps-9 pe-4 py-2 text-sm text-gray-900 dark:text-white placeholder:text-gray-400 dark:placeholder:text-gray-500 border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition"
            />
          </div>

          {/* Date toggle — segmented control */}
          <div className="flex items-center gap-0.5 p-0.5 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-white/5 text-sm shrink-0">
            {(["today", "week", "all"] as DateFilter[]).map((d) => (
              <button
                key={d}
                onClick={() => setDateFilter(d)}
                className={`press px-3 py-1.5 rounded-md font-medium transition-all ${
                  dateFilter === d
                    ? "bg-primary-600 text-white shadow-glow-sm"
                    : "text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
                }`}
              >
                {d === "today" ? "Today" : d === "week" ? "7 Days" : "All Time"}
              </button>
            ))}
          </div>

          {/* Modality */}
          {modalities.length > 1 && (
            <select
              value={modalityFilter}
              onChange={(e) => setModalityFilter(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition"
            >
              <option value="">All Modalities</option>
              {modalities.map((m) => <option key={m} value={m!}>{m}</option>)}
            </select>
          )}

          {/* Body Part */}
          {bodyParts.length > 0 && (
            <select
              value={bodyPartFilter}
              onChange={(e) => setBodyPartFilter(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition"
            >
              <option value="">All Body Parts</option>
              {bodyParts.map((bp) => <option key={bp} value={bp!}>{bp}</option>)}
            </select>
          )}

          {/* Referrer */}
          {referrers.length > 0 && (
            <select
              value={referrerFilter}
              onChange={(e) => setReferrerFilter(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition max-w-[180px]"
            >
              <option value="">All Referrers</option>
              {referrers.map((r) => <option key={r} value={r!}>{r}</option>)}
            </select>
          )}

          {/* Priority */}
          <select
            value={priorityFilter}
            onChange={(e) => setPriorityFilter(e.target.value)}
            className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition"
          >
            <option value="">All Priorities</option>
            <option value="STAT">STAT</option>
            <option value="HIGH">HIGH</option>
            <option value="NORMAL">NORMAL</option>
            <option value="ROUTINE">ROUTINE</option>
          </select>

          {/* AI Status */}
          <select
            value={aiStatusFilter}
            onChange={(e) => setAIStatusFilter(e.target.value as AIStatusFilter)}
            className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition"
          >
            <option value="any">Any AI Status</option>
            <option value="not_started">Not Started</option>
            <option value="in_progress">In Progress</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
          </select>

          {/* Clear */}
          {activeFilters > 0 && (
            <button
              onClick={clearFilters}
              className="press flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-white/5 hover:bg-gray-200 dark:hover:bg-white/10 rounded-lg transition-colors shrink-0"
            >
              <X className="w-3.5 h-3.5" />
              Clear ({activeFilters})
            </button>
          )}
        </div>
      </div>

      {/* Table ───────────────────────────────────────────────────────────── */}
      <div className="glass rounded-2xl overflow-hidden">
        <Table>
          <Caption>Worklist of studies with AI job status and reading workflow</Caption>
          <thead>
            <tr className="border-b border-border bg-black/5 dark:bg-white/5">
              <SortableTh label={strings.worklist.columnPriority} field="urgency" activeField={sortField} dir={sortDir} onSort={(f) => toggleSort(f as SortField)} />
              <SortableTh label={strings.worklist.columnPatient} field="patient_name" activeField={sortField} dir={sortDir} onSort={(f) => toggleSort(f as SortField)} />
              <Th>{strings.worklist.columnStudy}</Th>
              <SortableTh label={strings.worklist.columnDate} field="study_date" activeField={sortField} dir={sortDir} onSort={(f) => toggleSort(f as SortField)} />
              <SortableTh label="Last Run" field="last_run" activeField={sortField} dir={sortDir} onSort={(f) => toggleSort(f as SortField)} />
              <SortableTh label={strings.worklist.columnBodyPart} field="body_part_examined" activeField={sortField} dir={sortDir} onSort={(f) => toggleSort(f as SortField)} />
              <Th>{strings.worklist.columnAiStatus}</Th>
              <Th className="w-[170px]" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800 dark:divide-gray-800">
            {loading && studies.length === 0 ? (
              <TableSkeleton rows={7} columnWidths={[44, 130, 180, 90, 90, 88, 150, 112]} subtextColumns={[1, 3, 4]} />
            ) : filtered.length === 0 ? (
              <tr>
                <td colSpan={8}>
                  <EmptyState
                    icon={ClipboardList}
                    title={activeFilters > 0 ? strings.worklist.noStudiesMatch : strings.worklist.noStudiesYet}
                    description={
                      activeFilters > 0
                        ? strings.worklist.noStudiesMatchDescription
                        : strings.worklist.noStudiesYetDescription
                    }
                    action={
                      activeFilters > 0 ? (
                        <button
                          onClick={clearFilters}
                          className="press text-xs px-3 py-1.5 border border-gray-200 dark:border-gray-700 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-accent/5"
                        >
                          {strings.worklist.clearFilters}
                        </button>
                      ) : (
                        <Link
                          href="/upload"
                          className="btn-gradient text-xs px-3 py-1.5 rounded-lg font-medium"
                        >
                          {strings.worklist.uploadCta}
                        </Link>
                      )
                    }
                  />
                </td>
              </tr>
            ) : (
                paged.map((study) => {
                  const studyJobs    = jobs[study.study_instance_uid] || [];
                  const isRunning    = runningAI === study.study_instance_uid;
                  const urgency      = urgencyMap[study.study_instance_uid];
                  const priorityConf = PRIORITY_CONFIG[urgency?.priority ?? "ROUTINE"];
                  const latestJobs   = latestPerUsecase(studyJobs);
                  const completedJob = studyJobs.find((j) => j.status === "completed");

                  return (
                    <tr
                      key={study.study_instance_uid}
                      onClick={() => router.push(`/study/${study.study_instance_uid}`)}
                      className="group cursor-pointer transition-colors hover:bg-accent/5"
                    >
                      {/* Priority — a left-border accent stripe signals urgency at a
                          glance without washing the whole row in color (the badge +
                          icon below still carry the same signal for anyone who can't
                          see the stripe). Physical border side, not logical — a minor,
                          accepted RTL-mirroring gap for this one accent. */}
                      <td
                        className={`py-2 px-3 border-l-2 ${
                          urgency?.priority === "STAT"
                            ? "border-l-red-500"
                            : urgency?.priority === "HIGH"
                            ? "border-l-orange-400"
                            : "border-l-transparent"
                        }`}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <div className="flex flex-col gap-1.5">
                          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold border w-fit ${priorityConf.bg} ${priorityConf.text} ${priorityConf.border}`}>
                            {urgency?.priority === "STAT" && <AlertTriangle className="w-3 h-3" />}
                            {urgency?.priority === "HIGH" && <Zap className="w-3 h-3" />}
                            {priorityConf.label}
                          </span>
                          {urgency && (
                            <div className="flex items-center gap-1.5">
                              <div className="w-14 bg-gray-100 dark:bg-gray-800 rounded-full h-1.5">
                                <div
                                  className={`h-1.5 rounded-full transition-all ${
                                    urgency.score >= 75 ? "bg-red-500"
                                    : urgency.score >= 50 ? "bg-orange-400"
                                    : urgency.score >= 25 ? "bg-blue-400"
                                    : "bg-gray-300"
                                  }`}
                                  style={{ width: `${urgency.score}%` }}
                                />
                              </div>
                              <span className="text-[11px] text-gray-400 dark:text-gray-500 tabular-nums">{urgency.score}</span>
                            </div>
                          )}
                        </div>
                      </td>

                      {/* Patient */}
                      <td className="py-2.5 px-3">
                        <p className="font-semibold text-gray-900 dark:text-gray-100 transition-colors group-hover:text-primary-600 dark:group-hover:text-primary-400">
                          {formatPatientName(study.patient_name)}
                        </p>
                        <p className="text-xs text-gray-400 dark:text-gray-500">{study.patient_id || "—"}</p>
                      </td>

                      {/* Study */}
                      <td className="py-2 px-3 max-w-[200px]">
                        <p className="text-gray-800 dark:text-gray-200 truncate">{study.study_description || "—"}</p>
                        <div className="flex items-center gap-2 mt-0.5">
                          <p className="text-xs text-gray-400 dark:text-gray-500 font-mono truncate">
                            {study.accession_number || study.study_instance_uid.slice(0, 16) + "…"}
                          </p>
                          {study.series?.length > 0 && (
                            <span className="inline-flex items-center gap-0.5 text-[10px] text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded-full shrink-0">
                              <Layers className="w-2.5 h-2.5" />
                              {study.series.length}
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Date (study acquisition) */}
                      <td className="py-2 px-3 whitespace-nowrap">
                        <p className="text-gray-700 dark:text-gray-300">{formatDate(study.study_date)}</p>
                        <p className="text-xs text-gray-400 dark:text-gray-500 flex items-center gap-1 mt-0.5">
                          <Clock className="w-2.5 h-2.5 shrink-0" />
                          {relativeTime(study.created_at)}
                        </p>
                      </td>

                      {/* Last Run (most recent AI job) */}
                      <td className="py-2 px-3 whitespace-nowrap">
                        {studyJobs.length > 0 ? (
                          <>
                            <p className="text-gray-700 dark:text-gray-300">{relativeTime(studyJobs[0].created_at)}</p>
                            <p className="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">{formatDate(studyJobs[0].created_at)}</p>
                          </>
                        ) : (
                          <span className="text-sm text-gray-400 dark:text-gray-600">Never run</span>
                        )}
                      </td>

                      {/* Body Part — body region as the primary label, modality as a
                          restrained monospace tag beneath it. */}
                      <td className="py-2.5 px-3">
                        <div className="flex flex-col items-start gap-1">
                          {study.body_part_examined ? (
                            <span className="text-sm font-medium text-gray-800 dark:text-gray-200 capitalize">
                              {study.body_part_examined.toLowerCase()}
                            </span>
                          ) : (
                            <span className="text-sm text-gray-400 dark:text-gray-600">—</span>
                          )}
                          {study.modality && (
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded border border-gray-200 dark:border-white/10 bg-gray-50 dark:bg-white/5 text-[10px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 font-mono">
                              {study.modality}
                            </span>
                          )}
                        </div>
                      </td>

                      {/* AI Status */}
                      <td className="py-2 px-3" onClick={(e) => e.stopPropagation()}>
                        {latestJobs.length === 0 ? (
                          <span className="text-xs text-gray-400 dark:text-gray-500 italic">Not started</span>
                        ) : (
                          <div className="space-y-1">
                            {latestJobs.map((job) => {
                              const active = ACTIVE_STATUSES.includes(job.status);
                              const stale  = isStale(job);
                              const runCount = studyJobs.filter((j) => j.usecase_name === job.usecase_name).length;
                              return (
                                <div key={job.id} className="flex items-center gap-1.5 flex-wrap">
                                  <StatusBadge variant="job" status={stale ? "failed" : job.status} />
                                  <span className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 truncate max-w-[80px]">
                                    {job.usecase_name.replace(/_/g, " ")}
                                  </span>
                                  {active && !stale && (
                                    <span className="text-xs text-amber-600 dark:text-amber-400 font-medium tabular-nums">
                                      {(job.progress * 100).toFixed(0)}%
                                    </span>
                                  )}
                                  {stale && (
                                    <span
                                      className="text-[10px] font-medium text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 px-1.5 py-0.5 rounded-full flex items-center gap-0.5 shrink-0"
                                      title="No update in 15+ minutes — task may be stuck. Check worker logs."
                                    >
                                      <AlertTriangle className="w-2.5 h-2.5" />
                                      Stale
                                    </span>
                                  )}
                                  {job.status === "failed" && job.error_detail && (
                                    <span
                                      className="text-[10px] text-red-500 dark:text-red-400 cursor-help max-w-[120px] truncate"
                                      title={job.error_detail}
                                    >
                                      {job.error_detail.split("\n")[0].slice(0, 60)}
                                    </span>
                                  )}
                                  {/* Stop — all active statuses */}
                                  {active && (
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        handleCancelJob(job.id);
                                      }}
                                      disabled={cancellingJob === job.id}
                                      title="Stop this job"
                                      className="flex items-center gap-0.5 text-[10px] text-gray-500 dark:text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 bg-gray-100 dark:bg-gray-800 hover:bg-red-50 dark:hover:bg-red-950 px-1.5 py-0.5 rounded-full transition-colors shrink-0 disabled:opacity-60"
                                    >
                                      {cancellingJob === job.id ? (
                                        <>
                                          <RefreshCw className="w-2.5 h-2.5 animate-spin" /> Stopping…
                                        </>
                                      ) : (
                                        <>
                                          <Square className="w-2.5 h-2.5" /> Stop
                                        </>
                                      )}
                                    </button>
                                  )}
                                  {/* Retry — failed or cancelled */}
                                  {(job.status === "failed" || job.status === "cancelled") && (
                                    <button
                                      onClick={async (e) => {
                                        e.stopPropagation();
                                        try { await api.jobs.retry(job.id); await loadStudies(); } catch {}
                                      }}
                                      title="Retry this job"
                                      className="flex items-center gap-0.5 text-[10px] text-gray-500 dark:text-gray-400 dark:text-gray-500 hover:text-primary-600 bg-gray-100 dark:bg-gray-800 hover:bg-primary-50 px-1.5 py-0.5 rounded-full transition-colors shrink-0"
                                    >
                                      <RotateCcw className="w-2.5 h-2.5" /> Retry
                                    </button>
                                  )}
                                  {runCount > 1 && (
                                    <span className="text-[10px] text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded-full shrink-0">
                                      ×{runCount}
                                    </span>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </td>

                      {/* Actions */}
                      <td className="py-2 px-3" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <Link
                            href={`/study/${study.study_instance_uid}`}
                            className="press px-2.5 py-1.5 text-xs font-medium text-primary-600 dark:text-primary-400 border border-primary-200 dark:border-primary-800 rounded hover:bg-primary-50 dark:hover:bg-primary-950 transition-colors whitespace-nowrap"
                          >
                            View
                          </Link>
                          <button
                            onClick={() => setModalStudyUid(study.study_instance_uid)}
                            disabled={isRunning}
                            className="press flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-white bg-green-600 rounded hover:bg-green-700 disabled:opacity-50 transition-colors whitespace-nowrap"
                          >
                            {isRunning
                              ? <RefreshCw className="w-3 h-3 animate-spin motion-reduce:animate-none" />
                              : <PlayCircle className="w-3 h-3" />
                            }
                            Run AI
                          </button>
                          {study.patient_id && completedJob && (
                            <Link
                              href={`/admin/patients/${encodeURIComponent(study.patient_id)}/trend/${completedJob.usecase_name}`}
                              aria-label="View longitudinal trend"
                              title="Longitudinal trend"
                              className="p-1.5 text-purple-600 dark:text-purple-400 border border-purple-200 dark:border-purple-800 rounded hover:bg-purple-50 transition-colors"
                            >
                              <TrendingUp className="w-3.5 h-3.5" />
                            </Link>
                          )}
                          {/* Delete study */}
                          {confirmDelete === study.study_instance_uid ? (
                            <ConfirmDialog
                              tier="inline"
                              danger
                              confirmLabel={deletingStudy === study.study_instance_uid ? "…" : "Confirm"}
                              cancelLabel="Keep"
                              onConfirm={() => handleDeleteStudy(study.study_instance_uid)}
                              onCancel={() => setConfirmDelete(null)}
                            />
                          ) : (
                            <button
                              onClick={() => setConfirmDelete(study.study_instance_uid)}
                              aria-label="Remove study from platform"
                              title="Remove study from platform"
                              className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-950 rounded transition-colors"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
        </Table>

        {/* Table footer + pagination */}
        <div className="px-3.5 py-2.5 bg-black/5 dark:bg-white/5 border-t border-border flex flex-wrap items-center justify-between gap-3 text-xs text-gray-500 dark:text-gray-400">
          <span className="flex items-center gap-3">
            <span>
              Showing{" "}
              <span className="font-medium text-gray-700 dark:text-gray-300 tabular-nums">{startIdx}–{endIdx}</span> of{" "}
              <span className="font-medium text-gray-700 dark:text-gray-300 tabular-nums">{filtered.length}</span>
              {filtered.length !== total && <span className="text-gray-400 dark:text-gray-500"> (of {total})</span>}
              {activeFilters > 0 && (
                <span className="ms-1 text-primary-500">
                  · {activeFilters} filter{activeFilters > 1 ? "s" : ""} active
                </span>
              )}
            </span>
            {urgencyLoading && (
              <span className="flex items-center gap-1 text-gray-400 dark:text-gray-500">
                <RefreshCw className="w-3 h-3 animate-spin motion-reduce:animate-none" /> priority…
              </span>
            )}
          </span>

          <div className="flex items-center gap-3">
            {/* Rows per page */}
            <label className="flex items-center gap-1.5">
              <span className="hidden sm:inline">Rows</span>
              <select
                value={pageSize}
                onChange={(e) => setPageSize(Number(e.target.value))}
                aria-label="Rows per page"
                className="text-xs border border-gray-200 dark:border-gray-700 dark:bg-surface rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
              >
                {[5, 10, 20].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>

            {/* Pager */}
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={currentPage <= 1}
                aria-label="Previous page"
                className="press p-1.5 rounded-lg border border-border hover:bg-accent/5 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronLeft className="w-4 h-4 rtl:scale-x-[-1]" />
              </button>

              {/* Numbered page buttons */}
              {pageRange(currentPage, totalPages).map((p, i) =>
                p === "…" ? (
                  <span key={`gap-${i}`} className="px-1.5 text-gray-400 dark:text-gray-600 select-none">…</span>
                ) : (
                  <button
                    key={p}
                    onClick={() => setPage(p)}
                    aria-label={`Page ${p}`}
                    aria-current={p === currentPage ? "page" : undefined}
                    className={`press min-w-[28px] h-7 px-2 rounded-lg text-xs font-medium tabular-nums transition-colors ${
                      p === currentPage
                        ? "bg-primary-600 text-white shadow-glow-sm"
                        : "border border-border text-gray-600 dark:text-gray-300 hover:bg-accent/5"
                    }`}
                  >
                    {p}
                  </button>
                )
              )}

              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={currentPage >= totalPages}
                aria-label="Next page"
                className="press p-1.5 rounded-lg border border-border hover:bg-accent/5 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight className="w-4 h-4 rtl:scale-x-[-1]" />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Use-case selection modal */}
      {modalStudyUid && (
        <UsecaseModal
          studyUid={modalStudyUid}
          usecases={usecases}
          running={runningAI === modalStudyUid}
          onClose={() => setModalStudyUid(null)}
          onSelect={handleRunAI}
        />
      )}
    </div>
  );
}
