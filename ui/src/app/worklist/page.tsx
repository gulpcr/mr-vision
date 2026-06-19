"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, Study, Job, UseCase, UrgencyScore } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { formatDate, formatPatientName } from "@/lib/format";
import Link from "next/link";
import {
  Search, Upload, ChevronDown, ChevronUp, AlertTriangle, Zap,
  TrendingUp, RefreshCw, Clock, Layers, X, PlayCircle,
  ClipboardList, ChevronRight, Trash2, RotateCcw, Square,
  CheckCircle2,
} from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────────────

type SortField = "patient_name" | "study_date" | "body_part_examined" | "urgency";
type SortDir = "asc" | "desc";
type DateFilter = "today" | "week" | "all";
type AIStatusFilter = "any" | "not_started" | "in_progress" | "completed" | "failed";

const PRIORITY_CONFIG = {
  STAT:    { label: "STAT",    bg: "bg-red-100",    text: "text-red-700",    border: "border-red-200",    order: 0 },
  HIGH:    { label: "HIGH",    bg: "bg-orange-100", text: "text-orange-700", border: "border-orange-200", order: 1 },
  NORMAL:  { label: "NORMAL",  bg: "bg-blue-50",    text: "text-blue-600",   border: "border-blue-100",   order: 2 },
  ROUTINE: { label: "ROUTINE", bg: "bg-gray-50",    text: "text-gray-500",   border: "border-gray-100",   order: 3 },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

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

// The API returns UTC datetimes without a timezone suffix (e.g. "2026-06-08T17:06:39").
// JavaScript's Date() parses those as LOCAL time, making every timestamp appear
// offset by the user's UTC offset.  Appending 'Z' forces correct UTC interpretation.
function toUtcMs(dateStr: string | null | undefined): number {
  if (!dateStr) return 0;
  const s = dateStr.endsWith("Z") || dateStr.includes("+") ? dateStr : dateStr + "Z";
  return new Date(s).getTime();
}

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

// ── Stat Card ────────────────────────────────────────────────────────────────

function StatCard({
  label, value, sub, color = "gray",
}: {
  label: string; value: number | string; sub?: string;
  color?: "gray" | "red" | "amber" | "green";
}) {
  const ring = { gray: "border-gray-200", red: "border-red-200", amber: "border-amber-200", green: "border-green-200" }[color];
  const bg   = { gray: "bg-white", red: "bg-red-50", amber: "bg-amber-50", green: "bg-green-50" }[color];
  const val  = { gray: "text-gray-900", red: "text-red-700", amber: "text-amber-700", green: "text-green-700" }[color];
  return (
    <div className={`rounded-xl border ${ring} ${bg} px-5 py-4`}>
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-1">{label}</p>
      <p className={`text-2xl font-bold ${val}`}>{value}</p>
      {sub && <p className="text-xs mt-0.5 text-gray-500">{sub}</p>}
    </div>
  );
}

// ── Skeleton Row ─────────────────────────────────────────────────────────────

function SkeletonRow() {
  return (
    <tr className="border-b border-gray-50">
      {[44, 130, 180, 90, 88, 150, 112].map((w, i) => (
        <td key={i} className="py-4 px-4">
          <div className="h-4 bg-gray-100 rounded animate-pulse" style={{ width: w }} />
          {(i === 1 || i === 3) && (
            <div className="h-3 bg-gray-100 rounded animate-pulse mt-1.5" style={{ width: w * 0.55 }} />
          )}
        </td>
      ))}
    </tr>
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
    <div
      className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-2xl border border-gray-200 w-80 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
          <span className="font-semibold text-sm text-gray-800">Run AI Pipeline</span>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-2">
          <button
            onClick={() => onSelect(studyUid)}
            disabled={running}
            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm font-medium text-left rounded-lg text-primary-700 bg-primary-50 hover:bg-primary-100 disabled:opacity-50 transition-colors mb-1"
          >
            <Zap className="w-4 h-4 shrink-0" />
            <div>
              <div>Auto-Route</div>
              <div className="text-xs font-normal text-primary-500">Let the system pick the best pipeline</div>
            </div>
          </button>
          {usecases.filter((uc) => uc.enabled).length > 0 && (
            <>
              <div className="text-[10px] uppercase tracking-wider text-gray-400 px-3 py-1.5">
                Or choose a specific pipeline
              </div>
              {usecases.filter((uc) => uc.enabled).map((uc) => (
                <button
                  key={uc.name}
                  onClick={() => onSelect(studyUid, [uc.name])}
                  disabled={running}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left rounded-lg text-gray-700 hover:bg-gray-50 disabled:opacity-50 transition-colors"
                >
                  <ChevronRight className="w-3.5 h-3.5 text-gray-400 shrink-0" />
                  {uc.name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                </button>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Sort Icon ────────────────────────────────────────────────────────────────

function SortIcon({ field, active, dir }: { field: string; active: boolean; dir: SortDir }) {
  if (!active) return <ChevronDown className="w-3 h-3 text-gray-300" />;
  return dir === "asc"
    ? <ChevronUp className="w-3 h-3 text-primary-500" />
    : <ChevronDown className="w-3 h-3 text-primary-500" />;
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function WorklistPage() {
  const router = useRouter();

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
  const [priorityFilter, setPriorityFilter]   = useState("");
  const [dateFilter, setDateFilter]           = useState<DateFilter>("all");
  const [aiStatusFilter, setAIStatusFilter]   = useState<AIStatusFilter>("any");
  const [sortField, setSortField]             = useState<SortField>("urgency");
  const [sortDir, setSortDir]                 = useState<SortDir>("asc");

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
    setPriorityFilter(""); setDateFilter("all"); setAIStatusFilter("any");
  };

  // ── Derived data ──────────────────────────────────────────────────────────

  const bodyParts = Array.from(new Set(studies.map((s) => s.body_part_examined).filter(Boolean)));
  const modalities = Array.from(new Set(studies.map((s) => s.modality).filter(Boolean)));

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
      if (priorityFilter && urgencyMap[s.study_instance_uid]?.priority !== priorityFilter) return false;
      if (dateFilter !== "all" && s.study_date) {
        const d = new Date(s.study_date);
        if (dateFilter === "today" && d < todayStart) return false;
        if (dateFilter === "week"  && d < weekStart)  return false;
      }
      if (aiStatusFilter !== "any") {
        if (studyAIStatus(jobs[s.study_instance_uid] || []) !== aiStatusFilter) return false;
      }
      return true;
    })
    .sort((a, b) => {
      const dir = sortDir === "asc" ? 1 : -1;
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
    search, bodyPartFilter, modalityFilter, priorityFilter,
    dateFilter !== "all" ? "1" : "", aiStatusFilter !== "any" ? "1" : "",
  ].filter(Boolean).length;

  return (
    <div>
      {/* Header ──────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Worklist</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {total} stud{total === 1 ? "y" : "ies"}
            {lastRefreshed && (
              <span className="ml-2 text-gray-400">
                · Updated {relativeTime(lastRefreshed.toISOString())}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => loadStudies(true)}
            disabled={refreshing}
            title="Refresh worklist"
            className="p-2 text-gray-500 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors disabled:opacity-40"
          >
            <RefreshCw className={`w-4 h-4 ${refreshing ? "animate-spin" : ""}`} />
          </button>
          <Link
            href="/upload"
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
          >
            <Upload className="w-4 h-4" /> Upload DICOM
          </Link>
        </div>
      </div>

      {/* Stat Cards ──────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
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
        <div className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-xl px-4 py-3 mb-2">
          <CheckCircle2 className="w-4 h-4 text-green-500 shrink-0" />
          <p className="text-sm text-green-800 flex-1">
            <span className="font-semibold">Pipeline queued:</span> {runSuccess} — the worker will start processing shortly.
          </p>
          <button onClick={() => setRunSuccess(null)} className="text-green-400 hover:text-green-600">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Job dispatch error banner */}
      {jobError && (
        <div className="flex items-start gap-3 bg-red-50 border border-red-200 rounded-xl px-4 py-3 mb-2">
          <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-red-800">Failed to start AI pipeline</p>
            <p className="text-xs text-red-700 mt-0.5 break-words">{jobError}</p>
          </div>
          <button
            onClick={() => setJobError(null)}
            className="text-red-400 hover:text-red-600 shrink-0"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Filters ─────────────────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 mb-4">
        <div className="flex flex-wrap items-center gap-2 px-4 py-3">
          {/* Search */}
          <div className="relative flex-1 min-w-[220px]">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              ref={searchRef}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search patient, MRN, description, accession…  (/)"
              className="w-full pl-9 pr-4 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>

          {/* Date toggle */}
          <div className="flex rounded-lg border border-gray-200 overflow-hidden text-sm shrink-0">
            {(["today", "week", "all"] as DateFilter[]).map((d) => (
              <button
                key={d}
                onClick={() => setDateFilter(d)}
                className={`px-3 py-2 transition-colors ${
                  dateFilter === d ? "bg-primary-600 text-white" : "text-gray-600 hover:bg-gray-50"
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
              className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
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
              className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="">All Body Parts</option>
              {bodyParts.map((bp) => <option key={bp} value={bp!}>{bp}</option>)}
            </select>
          )}

          {/* Priority */}
          <select
            value={priorityFilter}
            onChange={(e) => setPriorityFilter(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
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
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
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
              className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors shrink-0"
            >
              <X className="w-3.5 h-3.5" />
              Clear ({activeFilters})
            </button>
          )}
        </div>
      </div>

      {/* Table ───────────────────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 bg-white">
                <th
                  className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider cursor-pointer hover:text-gray-700 select-none"
                  onClick={() => toggleSort("urgency")}
                >
                  <span className="flex items-center gap-1">
                    Priority
                    <SortIcon field="urgency" active={sortField === "urgency"} dir={sortDir} />
                  </span>
                </th>
                <th
                  className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider cursor-pointer hover:text-gray-700 select-none"
                  onClick={() => toggleSort("patient_name")}
                >
                  <span className="flex items-center gap-1">
                    Patient
                    <SortIcon field="patient_name" active={sortField === "patient_name"} dir={sortDir} />
                  </span>
                </th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                  Study
                </th>
                <th
                  className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider cursor-pointer hover:text-gray-700 select-none"
                  onClick={() => toggleSort("study_date")}
                >
                  <span className="flex items-center gap-1">
                    Date
                    <SortIcon field="study_date" active={sortField === "study_date"} dir={sortDir} />
                  </span>
                </th>
                <th
                  className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider cursor-pointer hover:text-gray-700 select-none"
                  onClick={() => toggleSort("body_part_examined")}
                >
                  <span className="flex items-center gap-1">
                    Body Part
                    <SortIcon field="body_part_examined" active={sortField === "body_part_examined"} dir={sortDir} />
                  </span>
                </th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                  AI Status
                </th>
                <th className="py-3 px-4 w-[170px]" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {loading && studies.length === 0 ? (
                Array.from({ length: 7 }).map((_, i) => <SkeletonRow key={i} />)
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <div className="py-16 flex flex-col items-center gap-3 text-center">
                      <ClipboardList className="w-10 h-10 text-gray-200" />
                      <div>
                        <p className="text-gray-500 font-medium">
                          {activeFilters > 0 ? "No studies match your filters" : "No studies yet"}
                        </p>
                        <p className="text-gray-400 text-xs mt-1">
                          {activeFilters > 0
                            ? "Try adjusting or clearing your filters"
                            : "Upload DICOM studies to get started"}
                        </p>
                      </div>
                      {activeFilters > 0 ? (
                        <button
                          onClick={clearFilters}
                          className="text-xs px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50"
                        >
                          Clear filters
                        </button>
                      ) : (
                        <Link
                          href="/upload"
                          className="text-xs px-3 py-1.5 bg-primary-600 text-white rounded-lg hover:bg-primary-700"
                        >
                          Upload DICOM
                        </Link>
                      )}
                    </div>
                  </td>
                </tr>
              ) : (
                filtered.map((study) => {
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
                      className={`cursor-pointer transition-colors ${
                        urgency?.priority === "STAT"
                          ? "bg-red-50/40 hover:bg-red-50"
                          : urgency?.priority === "HIGH"
                          ? "bg-orange-50/20 hover:bg-orange-50/40"
                          : "hover:bg-gray-50"
                      }`}
                    >
                      {/* Priority */}
                      <td className="py-3 px-4" onClick={(e) => e.stopPropagation()}>
                        <div className="flex flex-col gap-1.5">
                          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold border w-fit ${priorityConf.bg} ${priorityConf.text} ${priorityConf.border}`}>
                            {urgency?.priority === "STAT" && <AlertTriangle className="w-3 h-3" />}
                            {urgency?.priority === "HIGH" && <Zap className="w-3 h-3" />}
                            {priorityConf.label}
                          </span>
                          {urgency && (
                            <div className="flex items-center gap-1.5">
                              <div className="w-14 bg-gray-100 rounded-full h-1.5">
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
                              <span className="text-[11px] text-gray-400 tabular-nums">{urgency.score}</span>
                            </div>
                          )}
                        </div>
                      </td>

                      {/* Patient */}
                      <td className="py-3 px-4">
                        <p className="font-semibold text-gray-900">
                          {formatPatientName(study.patient_name)}
                        </p>
                        <p className="text-xs text-gray-400">{study.patient_id || "—"}</p>
                      </td>

                      {/* Study */}
                      <td className="py-3 px-4 max-w-[200px]">
                        <p className="text-gray-800 truncate">{study.study_description || "—"}</p>
                        <div className="flex items-center gap-2 mt-0.5">
                          <p className="text-xs text-gray-400 font-mono truncate">
                            {study.accession_number || study.study_instance_uid.slice(0, 16) + "…"}
                          </p>
                          {study.series?.length > 0 && (
                            <span className="inline-flex items-center gap-0.5 text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded-full shrink-0">
                              <Layers className="w-2.5 h-2.5" />
                              {study.series.length}
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Date */}
                      <td className="py-3 px-4 whitespace-nowrap">
                        <p className="text-gray-700">{formatDate(study.study_date)}</p>
                        <p className="text-xs text-gray-400 flex items-center gap-1 mt-0.5">
                          <Clock className="w-2.5 h-2.5 shrink-0" />
                          {relativeTime(study.created_at)}
                        </p>
                      </td>

                      {/* Body Part */}
                      <td className="py-3 px-4">
                        {study.body_part_examined ? (
                          <span className="px-2 py-0.5 bg-blue-50 text-blue-700 text-xs rounded-full font-medium">
                            {study.body_part_examined}
                          </span>
                        ) : (
                          <span className="text-gray-400 text-xs">—</span>
                        )}
                        {study.modality && (
                          <p className="text-[11px] text-gray-400 mt-0.5">{study.modality}</p>
                        )}
                      </td>

                      {/* AI Status */}
                      <td className="py-3 px-4" onClick={(e) => e.stopPropagation()}>
                        {latestJobs.length === 0 ? (
                          <span className="text-xs text-gray-400 italic">Not started</span>
                        ) : (
                          <div className="space-y-1">
                            {latestJobs.map((job) => {
                              const active = ACTIVE_STATUSES.includes(job.status);
                              const stale  = isStale(job);
                              const runCount = studyJobs.filter((j) => j.usecase_name === job.usecase_name).length;
                              return (
                                <div key={job.id} className="flex items-center gap-1.5 flex-wrap">
                                  <StatusBadge status={stale ? "failed" : job.status} />
                                  <span className="text-xs text-gray-500 truncate max-w-[80px]">
                                    {job.usecase_name.replace(/_/g, " ")}
                                  </span>
                                  {active && !stale && (
                                    <span className="text-xs text-amber-600 font-medium tabular-nums">
                                      {(job.progress * 100).toFixed(0)}%
                                    </span>
                                  )}
                                  {stale && (
                                    <span
                                      className="text-[10px] font-medium text-red-600 bg-red-50 border border-red-200 px-1.5 py-0.5 rounded-full flex items-center gap-0.5 shrink-0"
                                      title="No update in 15+ minutes — task may be stuck. Check worker logs."
                                    >
                                      <AlertTriangle className="w-2.5 h-2.5" />
                                      Stale
                                    </span>
                                  )}
                                  {job.status === "failed" && job.error_detail && (
                                    <span
                                      className="text-[10px] text-red-500 cursor-help max-w-[120px] truncate"
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
                                      className="flex items-center gap-0.5 text-[10px] text-gray-500 hover:text-red-600 bg-gray-100 hover:bg-red-50 px-1.5 py-0.5 rounded-full transition-colors shrink-0 disabled:opacity-60"
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
                                      className="flex items-center gap-0.5 text-[10px] text-gray-500 hover:text-primary-600 bg-gray-100 hover:bg-primary-50 px-1.5 py-0.5 rounded-full transition-colors shrink-0"
                                    >
                                      <RotateCcw className="w-2.5 h-2.5" /> Retry
                                    </button>
                                  )}
                                  {runCount > 1 && (
                                    <span className="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded-full shrink-0">
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
                      <td className="py-3 px-4" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <Link
                            href={`/study/${study.study_instance_uid}`}
                            className="px-2.5 py-1.5 text-xs font-medium text-primary-600 border border-primary-200 rounded-lg hover:bg-primary-50 transition-colors whitespace-nowrap"
                          >
                            View
                          </Link>
                          <button
                            onClick={() => setModalStudyUid(study.study_instance_uid)}
                            disabled={isRunning}
                            className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-white bg-green-600 rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors whitespace-nowrap"
                          >
                            {isRunning
                              ? <RefreshCw className="w-3 h-3 animate-spin" />
                              : <PlayCircle className="w-3 h-3" />
                            }
                            Run AI
                          </button>
                          {study.patient_id && completedJob && (
                            <Link
                              href={`/admin/patients/${encodeURIComponent(study.patient_id)}/trend/${completedJob.usecase_name}`}
                              title="Longitudinal trend"
                              className="p-1.5 text-purple-600 border border-purple-200 rounded-lg hover:bg-purple-50 transition-colors"
                            >
                              <TrendingUp className="w-3.5 h-3.5" />
                            </Link>
                          )}
                          {/* Delete study */}
                          {confirmDelete === study.study_instance_uid ? (
                            <div className="flex items-center gap-1">
                              <button
                                onClick={() => handleDeleteStudy(study.study_instance_uid)}
                                disabled={deletingStudy === study.study_instance_uid}
                                className="px-2 py-1 text-[10px] font-semibold text-white bg-red-600 rounded hover:bg-red-700 transition-colors"
                              >
                                {deletingStudy === study.study_instance_uid ? "…" : "Confirm"}
                              </button>
                              <button
                                onClick={() => setConfirmDelete(null)}
                                className="px-2 py-1 text-[10px] text-gray-500 bg-gray-100 rounded hover:bg-gray-200 transition-colors"
                              >
                                Keep
                              </button>
                            </div>
                          ) : (
                            <button
                              onClick={() => setConfirmDelete(study.study_instance_uid)}
                              title="Remove study from platform"
                              className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
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
          </table>
        </div>

        {/* Table footer */}
        <div className="px-4 py-3 bg-gray-50 border-t border-gray-100 flex items-center justify-between text-xs text-gray-500">
          <span>
            Showing{" "}
            <span className="font-medium text-gray-700">{filtered.length}</span> of{" "}
            <span className="font-medium text-gray-700">{total}</span> studies
            {activeFilters > 0 && (
              <span className="ml-1 text-primary-500">
                · {activeFilters} filter{activeFilters > 1 ? "s" : ""} active
              </span>
            )}
          </span>
          {urgencyLoading && (
            <span className="flex items-center gap-1 text-gray-400">
              <RefreshCw className="w-3 h-3 animate-spin" /> Loading priority scores…
            </span>
          )}
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
