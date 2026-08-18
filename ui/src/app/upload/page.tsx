"use client";

import { useState, useMemo, useRef, useEffect } from "react";
import { useOrthancStudies, useStudies } from "@/lib/hooks";
import { api, DicomUploadResult } from "@/lib/api";
import { formatDate, formatPatientName } from "@/lib/format";
import {
  Upload,
  RefreshCw,
  Search,
  CheckCircle,
  AlertCircle,
  ArrowRight,
  Network,
  Database,
  Brain,
  ChevronDown,
  ChevronUp,
  Info,
  FolderUp,
  FileUp,
  X,
  Loader2,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

// ── Modality badge colour ─────────────────────────────────────────────────────

function ModalityBadge({ modality }: { modality: string }) {
  const m = (modality || "").toUpperCase();
  const cfg: Record<string, string> = {
    MR:  "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border-blue-100 dark:border-blue-900",
    CT:  "bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 border-slate-200 dark:border-slate-700",
    PT:  "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300 border-amber-100 dark:border-amber-900",
    US:  "bg-teal-50 dark:bg-teal-950 text-teal-700 dark:text-teal-300 border-teal-100 dark:border-teal-900",
    DX:  "bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 border-gray-200 dark:border-gray-700",
    CR:  "bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 border-gray-200 dark:border-gray-700",
    NM:  "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300 border-green-100 dark:border-green-900",
    XA:  "bg-orange-50 dark:bg-orange-950 text-orange-700 dark:text-orange-300 border-orange-100 dark:border-orange-900",
  };
  return (
    <span className={`px-2 py-0.5 rounded border text-xs font-semibold ${cfg[m] ?? "bg-gray-50 dark:bg-gray-800 dark:bg-surface-raised text-gray-600 dark:text-gray-400 dark:text-gray-500 border-gray-200 dark:border-gray-700"}`}>
      {m || "—"}
    </span>
  );
}

// ── Local folder / file upload panel ──────────────────────────────────────────

// Files DICOM images never use — skip these so a dragged folder's stray PNGs,
// PDFs or OS junk don't get pushed to the PACS and reported as failures.
const SKIP_EXT = new Set([
  "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "txt", "pdf", "zip", "gz",
  "tar", "json", "xml", "html", "htm", "js", "css", "md", "csv", "xlsx", "doc", "docx",
]);

function isLikelyDicom(f: File): boolean {
  const name = f.name;
  if (!name || name.startsWith(".")) return false;          // hidden / OS files
  if (name.toLowerCase() === "dicomdir") return false;      // index record, not an image
  const dot = name.lastIndexOf(".");
  if (dot === -1) return true;                              // extensionless — typical DICOM
  return !SKIP_EXT.has(name.slice(dot + 1).toLowerCase());
}

// Upload this many instances per request. A study can be thousands of files, so
// one giant multipart request would time out / exhaust memory — batch instead.
const UPLOAD_BATCH = 40;

function LocalUploadPanel({ onIngested }: { onIngested: () => void }) {
  const folderRef = useRef<HTMLInputElement>(null);
  const filesRef = useRef<HTMLInputElement>(null);

  const [queue, setQueue] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [result, setResult] = useState<DicomUploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [showFailures, setShowFailures] = useState(false);

  // webkitdirectory / directory aren't valid JSX props — set them on the DOM node
  // so this input picks a whole folder (recursively) instead of single files.
  useEffect(() => {
    const el = folderRef.current;
    if (el) {
      el.setAttribute("webkitdirectory", "");
      el.setAttribute("directory", "");
      el.setAttribute("mozdirectory", "");
    }
  }, []);

  function addFiles(list: FileList | null) {
    if (!list) return;
    const incoming = Array.from(list).filter(isLikelyDicom);
    setResult(null);
    setError(null);
    setQueue((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}:${f.size}`));
      const merged = [...prev];
      for (const f of incoming) {
        const key = `${f.name}:${f.size}`;
        if (!seen.has(key)) { seen.add(key); merged.push(f); }
      }
      return merged;
    });
  }

  async function startUpload() {
    if (!queue.length || uploading) return;
    setUploading(true);
    setError(null);
    setResult(null);
    setShowFailures(false);
    setProgress({ done: 0, total: queue.length });

    const agg: DicomUploadResult = { uploaded: 0, failed: 0, studies_ingested: [], files: [] };
    const seenStudies = new Set<string>();
    // Each batch is isolated: a failed batch (network error, an unreadable file on
    // a flaky drive, a server hiccup) is recorded and we move on, so one bad batch
    // never aborts the rest of the folder.
    for (let i = 0; i < queue.length; i += UPLOAD_BATCH) {
      const chunk = queue.slice(i, i + UPLOAD_BATCH);
      try {
        const r = await api.studies.upload(chunk);
        agg.uploaded += r.uploaded;
        agg.failed += r.failed;
        agg.files.push(...r.files);
        for (const s of r.studies_ingested) {
          if (!seenStudies.has(s.study_instance_uid)) {
            seenStudies.add(s.study_instance_uid);
            agg.studies_ingested.push(s);
          }
        }
      } catch (e: any) {
        agg.failed += chunk.length;
        for (const f of chunk) {
          agg.files.push({ filename: f.name, status: "error", detail: e.message || "Batch failed" });
        }
      }
      setProgress({ done: Math.min(i + UPLOAD_BATCH, queue.length), total: queue.length });
      setResult({ ...agg });
    }
    setQueue([]);
    onIngested();
    setUploading(false);
  }

  const pct = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;

  return (
    <div className="bg-white dark:bg-surface border border-gray-200 dark:border-gray-700 rounded-xl px-5 py-4">
      <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
        Upload from this computer
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Pick a study <strong>folder</strong> (or individual <code className="text-[11px] bg-gray-100 dark:bg-gray-800 px-1 rounded">.dcm</code> files) and they&apos;ll be pushed to the PACS and ingested — no need to open Orthanc.
      </p>

      {/* Drop zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files); }}
        className={`rounded-lg border-2 border-dashed px-4 py-6 text-center transition-colors ${
          dragOver
            ? "border-primary-400 bg-primary-50 dark:bg-primary-950/30"
            : "border-gray-200 dark:border-gray-700"
        }`}
      >
        <Upload className="w-6 h-6 mx-auto text-gray-400 dark:text-gray-500 mb-2" />
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
          Drag DICOM files here, or
        </p>
        <div className="flex items-center justify-center gap-2">
          <button
            type="button"
            onClick={() => folderRef.current?.click()}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-colors"
          >
            <FolderUp className="w-3.5 h-3.5" /> Select folder
          </button>
          <button
            type="button"
            onClick={() => filesRef.current?.click()}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-colors"
          >
            <FileUp className="w-3.5 h-3.5" /> Select files
          </button>
        </div>
        <input ref={folderRef} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
        <input ref={filesRef} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
      </div>

      {/* Queue + upload action */}
      {queue.length > 0 && !uploading && (
        <div className="flex items-center justify-between mt-3">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            <strong className="text-gray-900 dark:text-gray-100">{queue.length}</strong> file{queue.length === 1 ? "" : "s"} ready
            <button onClick={() => setQueue([])} className="ml-2 text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 inline-flex items-center gap-0.5">
              <X className="w-3 h-3" /> clear
            </button>
          </p>
          <button
            onClick={startUpload}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
          >
            <Upload className="w-4 h-4" /> Upload &amp; ingest
          </button>
        </div>
      )}

      {/* Progress */}
      {uploading && (
        <div className="mt-3">
          <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
            <span className="flex items-center gap-1.5"><Loader2 className="w-3.5 h-3.5 animate-spin motion-reduce:animate-none" /> Uploading…</span>
            <span>{progress.done} / {progress.total}</span>
          </div>
          <div className="h-1.5 w-full bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
            <div className="h-full bg-primary-600 transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {/* Result summary */}
      {result && !uploading && (
        <div className="mt-3 space-y-2">
          <p className="text-sm text-green-700 dark:text-green-400 flex items-center gap-1.5">
            <CheckCircle className="w-4 h-4" />
            {result.uploaded} file{result.uploaded === 1 ? "" : "s"} uploaded
            {result.failed > 0 && <span className="text-amber-600 dark:text-amber-400">· {result.failed} skipped/failed</span>}
          </p>
          {result.studies_ingested.length > 0 && (
            <div className="space-y-1">
              {result.studies_ingested.map((s) => (
                <div key={s.study_instance_uid} className="flex items-center justify-between text-sm border border-gray-100 dark:border-gray-800 rounded-lg px-3 py-1.5">
                  <span className="text-gray-700 dark:text-gray-300 truncate">
                    {s.error
                      ? <span className="text-red-600 dark:text-red-400">Ingest failed: {s.error}</span>
                      : <>{formatPatientName(s.patient_name || "")} <span className="text-gray-400 dark:text-gray-500">· {s.modality || "—"} · {s.series_count} series</span></>}
                  </span>
                  {!s.error && (
                    <Link href={`/study/${s.study_instance_uid}`} className="flex items-center gap-1 text-xs font-medium text-primary-600 dark:text-primary-400 whitespace-nowrap ml-2">
                      Open <ArrowRight className="w-3 h-3" />
                    </Link>
                  )}
                </div>
              ))}
            </div>
          )}
          {result.failed > 0 && (
            <div>
              <button onClick={() => setShowFailures((v) => !v)} className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 inline-flex items-center gap-1">
                {showFailures ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />} {result.failed} skipped/failed file{result.failed === 1 ? "" : "s"}
              </button>
              {showFailures && (
                <ul className="mt-1 text-xs text-gray-500 dark:text-gray-400 space-y-0.5 max-h-40 overflow-y-auto">
                  {result.files.filter((f) => f.status !== "uploaded").map((f, i) => (
                    <li key={`${f.filename}-${i}`} className="truncate"><span className="font-mono">{f.filename}</span> — {f.detail}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      {error && (
        <p className="mt-3 text-sm text-red-600 dark:text-red-400 flex items-start gap-1.5">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" /> {error}
        </p>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function UploadPage() {
  const { data: orthancStudies, isLoading, mutate } = useOrthancStudies();
  const { data: platformData } = useStudies();
  const router = useRouter();

  const [search, setSearch] = useState("");
  const [ingesting, setIngesting] = useState<string | null>(null);
  const [ingestedUids, setIngestedUids] = useState<string[]>([]);
  const [ingestError, setIngestError] = useState<{ uid: string; msg: string } | null>(null);

  const [manualUid, setManualUid] = useState("");
  const [manualLoading, setManualLoading] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);
  const [showManual, setShowManual] = useState(false);

  // Build a set of UIDs already in the platform for quick lookup
  const platformUidSet = useMemo(
    () => new Set((platformData?.studies ?? []).map((s) => s.study_instance_uid)),
    [platformData]
  );

  const filtered = (orthancStudies ?? []).filter((s) => {
    const q = search.toLowerCase();
    return (
      !q ||
      s.patient_name?.toLowerCase().includes(q) ||
      s.patient_id?.toLowerCase().includes(q) ||
      s.study_description?.toLowerCase().includes(q) ||
      s.modality?.toLowerCase().includes(q)
    );
  });

  async function handleIngest(uid: string) {
    setIngesting(uid);
    setIngestError(null);
    try {
      await api.studies.ingest(uid);
      setIngestedUids((prev) => [...prev, uid]);
    } catch (e: any) {
      setIngestError({ uid, msg: e.message || "Ingest failed" });
    } finally {
      setIngesting(null);
    }
  }

  async function handleManualIngest(e: React.FormEvent) {
    e.preventDefault();
    const uid = manualUid.trim();
    if (!uid) return;
    const uidPattern = /^[0-9][0-9.]{0,62}[0-9]$/;
    if (!uidPattern.test(uid)) {
      setManualError("Invalid Study Instance UID — must contain only digits and dots, starting and ending with a digit. Example: 1.2.840.113619.2.55.3");
      return;
    }
    setManualLoading(true);
    setManualError(null);
    try {
      const study = await api.studies.ingest(uid);
      router.push(`/study/${study.study_instance_uid}`);
    } catch (err: any) {
      setManualError(err.message || "Failed to ingest study");
    } finally {
      setManualLoading(false);
    }
  }

  const alreadyInPlatform = (uid: string) =>
    platformUidSet.has(uid) || ingestedUids.includes(uid);

  return (
    <div className="space-y-6">

      {/* ── Page header ──────────────────────────────────────────────────── */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Upload / Ingest DICOM</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500 mt-1">
          Bring studies from your PACS into the AI platform for analysis.
        </p>
      </div>

      {/* ── How it works ─────────────────────────────────────────────────── */}
      <div className="bg-blue-50 dark:bg-blue-950 border border-blue-100 dark:border-blue-900 rounded-xl px-5 py-4">
        <p className="text-xs font-semibold text-blue-700 dark:text-blue-300 uppercase tracking-wider mb-3 flex items-center gap-1.5">
          <Info className="w-3.5 h-3.5" /> How it works
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="flex gap-3">
            <div className="shrink-0 w-7 h-7 rounded-full bg-white dark:bg-surface border border-blue-200 dark:border-blue-800 flex items-center justify-center text-xs font-bold text-blue-600 dark:text-blue-400">1</div>
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <Network className="w-5 h-5 text-blue-500 dark:text-blue-400" />
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">Scanner → Orthanc PACS</p>
              </div>
              <p className="text-xs text-gray-600 dark:text-gray-400 dark:text-gray-500 leading-relaxed">DICOM files arrive from the scanner over the DICOM network and are stored in Orthanc (your on-site PACS). Nothing to do here — this happens automatically.</p>
            </div>
          </div>
          <div className="flex gap-3">
            <div className="shrink-0 w-7 h-7 rounded-full bg-white dark:bg-surface border border-blue-200 dark:border-blue-800 flex items-center justify-center text-xs font-bold text-blue-600 dark:text-blue-400">2</div>
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <Database className="w-5 h-5 text-primary-500" />
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">Ingest into Platform</p>
              </div>
              <p className="text-xs text-gray-600 dark:text-gray-400 dark:text-gray-500 leading-relaxed">Click <strong>Ingest</strong> next to a study below. The platform reads the DICOM metadata from Orthanc and registers the study so AI can analyse it.</p>
            </div>
          </div>
          <div className="flex gap-3">
            <div className="shrink-0 w-7 h-7 rounded-full bg-white dark:bg-surface border border-blue-200 dark:border-blue-800 flex items-center justify-center text-xs font-bold text-blue-600 dark:text-blue-400">3</div>
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <Brain className="w-5 h-5 text-green-500 dark:text-green-400" />
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">Run AI Pipeline</p>
              </div>
              <p className="text-xs text-gray-600 dark:text-gray-400 dark:text-gray-500 leading-relaxed">Open the study, click <strong>Run AI</strong>, and select a pipeline (e.g. Brain MRI, PET/CT). Results appear on the study page when processing is complete.</p>
            </div>
          </div>
        </div>
      </div>

      {/* ── Upload from this computer ─────────────────────────────────────── */}
      <LocalUploadPanel onIngested={() => { mutate(); }} />

      {/* ── DICOM send config ─────────────────────────────────────────────── */}
      <div className="bg-white dark:bg-surface border border-gray-200 dark:border-gray-700 rounded-xl px-5 py-4">
        <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-3">
          Configure your scanner to send DICOMs here
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          {[
            { label: "AE Title",          value: "ORTHANC" },
            { label: "Host / IP",         value: "103.93.216.37" },
            { label: "DICOM Port",        value: "4242" },
            { label: "HTTP (Orthanc UI)", value: "8042" },
          ].map(({ label, value }) => (
            <div key={label}>
              <p className="text-xs text-gray-400 dark:text-gray-500 mb-0.5">{label}</p>
              <p className="font-mono font-semibold text-gray-900 dark:text-gray-100 bg-gray-50 dark:bg-gray-800 dark:bg-surface-raised border border-gray-200 dark:border-gray-700 rounded px-2 py-1 text-xs">
                {value}
              </p>
            </div>
          ))}
        </div>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-3">
          After the scanner sends a study and Orthanc marks it "stable", it appears in the table below automatically. Hit <strong>Refresh</strong> if you don't see it yet.
        </p>
      </div>

      {/* ── Orthanc PACS Browser ──────────────────────────────────────────── */}
      <div className="bg-white dark:bg-surface rounded-xl shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <div>
            <h2 className="font-semibold text-gray-900 dark:text-gray-100">
              Studies in Orthanc PACS
              {orthancStudies && (
                <span className="ml-2 text-xs font-normal text-gray-400 dark:text-gray-500">
                  {orthancStudies.length} available
                </span>
              )}
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 mt-0.5">
              Studies with a <span className="text-green-600 dark:text-green-400 font-medium">green tick</span> are already in the platform and ready for AI analysis.
            </p>
          </div>
          <button
            onClick={() => mutate()}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 dark:hover:bg-surface-raised transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Refresh
          </button>
        </div>

        {/* Search */}
        <div className="px-5 py-3 border-b border-gray-100 dark:border-gray-800">
          <div className="relative">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 dark:text-gray-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter by patient name, MRN, description or modality…"
              className="w-full pl-9 pr-4 py-2 text-sm border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
        </div>

        {/* Table */}
        {isLoading ? (
          <div className="p-10 text-center text-sm text-gray-400 dark:text-gray-500">Loading from Orthanc…</div>
        ) : !filtered.length ? (
          <div className="p-10 text-center space-y-2">
            <p className="text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500 font-medium">
              {search ? "No studies match your filter" : "No studies in Orthanc yet"}
            </p>
            {!search && (
              <p className="text-xs text-gray-400 dark:text-gray-500">
                Send DICOMs from your scanner using the connection details above, or ask your PACS administrator.
              </p>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-white/5">
                  <th className="text-left px-5 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Patient</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">MRN</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Date</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Description</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Modality</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Series</th>
                  <th className="px-5 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider text-right">Status / Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
                {filtered.map((s) => {
                  const inPlatform = alreadyInPlatform(s.study_instance_uid);
                  const isIngesting = ingesting === s.study_instance_uid;
                  const hasError = ingestError?.uid === s.study_instance_uid;

                  return (
                    <tr
                      key={s.orthanc_id}
                      className={`transition-colors ${inPlatform ? "bg-green-50/40 dark:bg-green-950/20" : "hover:bg-gray-50 dark:hover:bg-white/5"}`}
                    >
                      <td className="px-5 py-3">
                        <p className="font-semibold text-gray-900 dark:text-gray-100">{formatPatientName(s.patient_name)}</p>
                      </td>
                      <td className="px-3 py-3 text-gray-600 dark:text-gray-300 font-mono text-xs">{s.patient_id || "—"}</td>
                      <td className="px-3 py-3 text-gray-600 dark:text-gray-300 whitespace-nowrap">{formatDate(s.study_date)}</td>
                      <td className="px-3 py-3 text-gray-700 dark:text-gray-300 max-w-[200px]">
                        <span className="truncate block" title={s.study_description || ""}>
                          {s.study_description || <span className="text-gray-400 dark:text-gray-500 italic">No description</span>}
                        </span>
                      </td>
                      <td className="px-3 py-3">
                        <ModalityBadge modality={s.modality} />
                      </td>
                      <td className="px-3 py-3 text-gray-700 dark:text-gray-300 text-center">{s.series_count}</td>
                      <td className="px-5 py-3 text-right">
                        {inPlatform ? (
                          <div className="flex items-center justify-end gap-2">
                            <span className="flex items-center gap-1 text-xs text-green-600 dark:text-green-400 font-medium">
                              <CheckCircle className="w-3.5 h-3.5" />
                              In Platform
                            </span>
                            <Link
                              href={`/study/${s.study_instance_uid}`}
                              className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-primary-600 dark:text-primary-400 border border-primary-200 dark:border-primary-800 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-950 transition-colors whitespace-nowrap"
                            >
                              Open <ArrowRight className="w-3 h-3" />
                            </Link>
                          </div>
                        ) : hasError ? (
                          <div className="flex items-center justify-end gap-1.5">
                            <span className="flex items-center gap-1 text-xs text-red-600 dark:text-red-400">
                              <AlertCircle className="w-3.5 h-3.5" />
                              {ingestError!.msg}
                            </span>
                            <button
                              onClick={() => handleIngest(s.study_instance_uid)}
                              className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 underline"
                            >
                              Retry
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => handleIngest(s.study_instance_uid)}
                            disabled={isIngesting}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors ml-auto"
                          >
                            {isIngesting ? (
                              <><RefreshCw className="w-3 h-3 animate-spin motion-reduce:animate-none" /> Ingesting…</>
                            ) : (
                              <><Upload className="w-3 h-3" /> Ingest</>
                            )}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Manual UID (collapsed by default) ────────────────────────────── */}
      <div className="bg-white dark:bg-surface rounded-xl border border-gray-200 dark:border-gray-700">
        <button
          onClick={() => setShowManual(!showManual)}
          className="w-full flex items-center justify-between px-5 py-4 text-left hover:bg-gray-50 dark:hover:bg-gray-800 dark:hover:bg-surface-raised transition-colors rounded-xl"
        >
          <div>
            <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Ingest by Study Instance UID</p>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
              Use this if the study doesn't appear above (e.g. sent via a worklist or external PACS).
            </p>
          </div>
          {showManual ? <ChevronUp className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
        </button>

        {showManual && (
          <div className="px-5 pb-5 border-t border-gray-100 dark:border-gray-800 pt-4">
            <p className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 mb-3">
              The <strong>Study Instance UID</strong> is a DICOM tag (0020,000D) that uniquely identifies the study. You can find it in the Orthanc web UI at{" "}
              <code className="text-xs bg-gray-100 dark:bg-gray-800 px-1 rounded">http://103.93.216.37:8042</code> or in the scanner's DICOM worklist.
            </p>
            <form onSubmit={handleManualIngest} className="flex gap-3">
              <input
                type="text"
                value={manualUid}
                onChange={(e) => { setManualUid(e.target.value); setManualError(null); }}
                placeholder="1.2.840.113619.2.55.3…"
                className="flex-1 px-4 py-2 text-sm font-mono border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
              <button
                type="submit"
                disabled={manualLoading || !manualUid.trim()}
                className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors whitespace-nowrap"
              >
                <Upload className="w-4 h-4" />
                {manualLoading ? "Ingesting…" : "Ingest & Open"}
              </button>
            </form>
            {manualError && (
              <p className="mt-2 text-sm text-red-600 dark:text-red-400 flex items-start gap-1.5">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                {manualError}
              </p>
            )}
          </div>
        )}
      </div>

    </div>
  );
}
