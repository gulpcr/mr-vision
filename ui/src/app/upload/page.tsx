"use client";

import { useState, useMemo, useRef, useEffect } from "react";
import { useOrthancStudies, useStudies } from "@/lib/hooks";
import { api, DicomUploadResult } from "@/lib/api";
import { formatDate, formatPatientName } from "@/lib/format";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
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
  Hash,
  Clock,
  FileStack,
  RotateCcw,
  Trash2,
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

// ── Source selector card ───────────────────────────────────────────────────────
// One clickable "source" tile per way a study can enter the platform — Local
// upload, the on-site PACS, the DICOM network (AE/port config), or a manual
// Study Instance UID. Mirrors a standard PACS ops-console pattern: pick a
// source, then work in a single focused panel below instead of scrolling past
// every source at once.

function SourceCard({
  icon: Icon, label, desc, meta, active, onClick,
}: {
  icon: React.ElementType; label: string; desc: string; meta?: string;
  active: boolean; onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`group text-left glass rounded-2xl p-4 hover-lift transition-colors ${
        active ? "ring-2 ring-accent border-accent/60 bg-accent/5" : ""
      }`}
    >
      <span
        className={`grid place-items-center w-10 h-10 rounded-xl mb-3 transition-transform duration-300 group-hover:scale-110 ${
          active ? "bg-accent-gradient text-white shadow-glow-sm" : "bg-accent/10 ring-1 ring-accent/20 text-accent"
        }`}
      >
        <Icon className="w-5 h-5" />
      </span>
      <p className="text-sm font-semibold text-gray-900 dark:text-gray-100">{label}</p>
      <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{desc}</p>
      {meta && <p className="text-[11px] text-accent font-medium mt-1.5">{meta}</p>}
    </button>
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

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

// Upload this many instances per request. A study can be thousands of files, so
// one giant multipart request would time out / exhaust memory — batch instead.
const UPLOAD_BATCH = 40;

type FileStatus = "queued" | "uploading" | "uploaded" | "failed";
interface QueuedFile {
  id: string; file: File; status: FileStatus; detail?: string;
  // This item's index in the aggregate `result.files` array (set once its
  // batch resolves) — lets a later single-file retry patch exactly that slot
  // instead of matching by filename, which collides across series subfolders.
  resultIndex?: number;
}

function FileStatusIcon({ status }: { status: FileStatus }) {
  if (status === "uploaded") return <CheckCircle className="w-4 h-4 text-green-500 shrink-0" />;
  if (status === "failed") return <AlertCircle className="w-4 h-4 text-red-500 shrink-0" />;
  if (status === "uploading") return <Loader2 className="w-4 h-4 text-primary-500 animate-spin motion-reduce:animate-none shrink-0" />;
  return <Clock className="w-4 h-4 text-gray-300 dark:text-gray-600 shrink-0" />;
}

function LocalUploadPanel({ onIngested }: { onIngested: () => void }) {
  const folderRef = useRef<HTMLInputElement>(null);
  const filesRef = useRef<HTMLInputElement>(null);

  const [queue, setQueue] = useState<QueuedFile[]>([]);
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
      const seen = new Set(prev.map((q) => `${q.file.name}:${q.file.size}`));
      const merged = [...prev];
      for (const f of incoming) {
        const key = `${f.name}:${f.size}`;
        if (!seen.has(key)) { seen.add(key); merged.push({ id: key, file: f, status: "queued" }); }
      }
      return merged;
    });
  }

  function removeFile(id: string) {
    setQueue((prev) => prev.filter((q) => q.id !== id));
  }

  // Match a batch's per-file results back onto the queue rows by ARRAY
  // POSITION, not filename. A whole-folder DICOM upload routinely has the same
  // basename (e.g. "IM00001") in every series subfolder — the browser's File
  // object strips the subfolder path, so filenames collide constantly and are
  // NOT a safe join key. The backend (study_service.upload_and_ingest) appends
  // exactly one result per input file in the order received, so positional
  // zip against the same-order `ids` we sent is the only reliable match.
  function applyFileResults(ids: string[], files: DicomUploadResult["files"], baseIndex: number) {
    const resultById = new Map(ids.map((id, i) => [id, { r: files[i], idx: baseIndex + i }]));
    setQueue((prev) => prev.map((q) => {
      const entry = resultById.get(q.id);
      if (!entry) return q;
      const { r, idx } = entry;
      return r.status === "uploaded"
        ? { ...q, status: "uploaded", resultIndex: idx }
        : { ...q, status: "failed", detail: r.detail, resultIndex: idx };
    }));
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
      const chunkIds = chunk.map((q) => q.id);
      setQueue((prev) => prev.map((q) => chunkIds.includes(q.id) ? { ...q, status: "uploading" } : q));
      try {
        const r = await api.studies.upload(chunk.map((q) => q.file));
        agg.uploaded += r.uploaded;
        agg.failed += r.failed;
        const baseIndex = agg.files.length;
        agg.files.push(...r.files);
        applyFileResults(chunkIds, r.files, baseIndex);
        for (const s of r.studies_ingested) {
          if (!seenStudies.has(s.study_instance_uid)) {
            seenStudies.add(s.study_instance_uid);
            agg.studies_ingested.push(s);
          }
        }
      } catch (e: any) {
        agg.failed += chunk.length;
        const detail = e.message || "Batch failed";
        const baseIndex = agg.files.length;
        for (const q of chunk) {
          agg.files.push({ filename: q.file.name, status: "error", detail });
        }
        const resultIndexById = new Map(chunkIds.map((cid, i) => [cid, baseIndex + i]));
        setQueue((prev) => prev.map((q) =>
          chunkIds.includes(q.id) ? { ...q, status: "failed", detail, resultIndex: resultIndexById.get(q.id) } : q
        ));
      }
      setProgress({ done: Math.min(i + UPLOAD_BATCH, queue.length), total: queue.length });
      setResult({ ...agg });
    }
    // Successfully uploaded files fold into the result summary below; only the
    // stragglers that need attention stay visible, each with a Retry action.
    setQueue((prev) => prev.filter((q) => q.status === "failed"));
    onIngested();
    setUploading(false);
  }

  async function retryFile(id: string) {
    const item = queue.find((q) => q.id === id);
    if (!item || item.status === "uploading") return;
    setQueue((prev) => prev.map((q) => (q.id === id ? { ...q, status: "uploading" } : q)));
    try {
      const r = await api.studies.upload([item.file]);
      // Exactly one file was sent, so the response has exactly one entry —
      // positional, unambiguous regardless of filename collisions.
      const fileResult = r.files[0];
      const ok = fileResult?.status === "uploaded";
      setQueue((prev) =>
        ok
          ? prev.filter((q) => q.id !== id)
          : prev.map((q) => (q.id === id ? { ...q, status: "failed", detail: fileResult?.detail } : q))
      );
      setResult((prev) => {
        if (!prev) return prev;
        // Patch by the item's own slot in the aggregate array — filename alone
        // is not unique (duplicate basenames across series subfolders), so
        // matching by name here would risk overwriting an unrelated file's
        // already-correct result.
        const next = {
          ...prev,
          files: item.resultIndex !== undefined
            ? prev.files.map((f, idx) => (idx === item.resultIndex ? (fileResult || f) : f))
            : prev.files,
        };
        if (ok) { next.uploaded += 1; next.failed = Math.max(0, next.failed - 1); }
        const seen = new Set(next.studies_ingested.map((s) => s.study_instance_uid));
        for (const s of r.studies_ingested) {
          if (!seen.has(s.study_instance_uid)) { seen.add(s.study_instance_uid); next.studies_ingested = [...next.studies_ingested, s]; }
        }
        return next;
      });
      if (ok) onIngested();
    } catch (e: any) {
      setQueue((prev) => prev.map((q) => (q.id === id ? { ...q, status: "failed", detail: e.message || "Retry failed" } : q)));
    }
  }

  const pct = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;
  const queueBytes = queue.reduce((a, q) => a + q.file.size, 0);

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

      {/* Queue — a live per-file list, not just one aggregate bar. Rows carry
          real, non-fabricated status: queued → uploading (while their batch is
          in flight) → uploaded / failed, taken straight from the per-file
          result the upload endpoint returns for that batch. */}
      {queue.length > 0 && (
        <div className="mt-3 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
          <div className="flex items-center justify-between gap-3 px-3 py-2.5 bg-gray-50 dark:bg-white/5 border-b border-gray-200 dark:border-gray-700">
            <div className="flex items-center gap-2 min-w-0">
              <FileStack className="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" />
              <p className="text-sm text-gray-700 dark:text-gray-300 truncate">
                <strong className="text-gray-900 dark:text-gray-100">{queue.length}</strong> file{queue.length === 1 ? "" : "s"}
                <span className="text-gray-400 dark:text-gray-500"> · {formatBytes(queueBytes)}</span>
              </p>
            </div>
            {!uploading ? (
              <div className="flex items-center gap-2 shrink-0">
                <button onClick={() => setQueue([])} className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 inline-flex items-center gap-0.5">
                  <X className="w-3 h-3" /> Clear
                </button>
                <button
                  onClick={startUpload}
                  className="press flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
                >
                  <Upload className="w-3.5 h-3.5" /> Upload &amp; ingest
                </button>
              </div>
            ) : (
              <span className="flex items-center gap-1.5 text-xs font-semibold text-primary-600 dark:text-primary-400 shrink-0 tabular-nums">
                <Loader2 className="w-3.5 h-3.5 animate-spin motion-reduce:animate-none" /> {progress.done} / {progress.total} · {pct}%
              </span>
            )}
          </div>

          {uploading && (
            <div className="h-1 w-full bg-gray-100 dark:bg-gray-800 overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-primary-500 to-primary-600 transition-all duration-300"
                style={{ width: `${pct}%` }}
              />
            </div>
          )}

          <ul className="max-h-64 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800">
            {queue.map((item) => (
              <li key={item.id} className="group flex items-center gap-2.5 px-3 py-2">
                <FileStatusIcon status={item.status} />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-mono text-gray-700 dark:text-gray-300 truncate" title={item.file.name}>
                    {item.file.name}
                  </p>
                  {item.status === "failed" && item.detail && (
                    <p className="text-[11px] text-red-500 dark:text-red-400 truncate">{item.detail}</p>
                  )}
                </div>
                <span className="text-[11px] text-gray-400 dark:text-gray-500 shrink-0 tabular-nums">
                  {formatBytes(item.file.size)}
                </span>
                {item.status === "failed" ? (
                  <button
                    onClick={() => retryFile(item.id)}
                    className="flex items-center gap-1 text-[11px] font-medium text-primary-600 dark:text-primary-400 hover:underline shrink-0"
                  >
                    <RotateCcw className="w-3 h-3" /> Retry
                  </button>
                ) : !uploading && item.status === "queued" && (
                  <button
                    onClick={() => removeFile(item.id)}
                    aria-label={`Remove ${item.file.name}`}
                    className="opacity-0 group-hover:opacity-100 text-gray-300 dark:text-gray-600 hover:text-gray-500 dark:hover:text-gray-400 transition-opacity shrink-0"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </li>
            ))}
          </ul>
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
  const [activeSource, setActiveSource] = useState<"local" | "pacs" | "network" | "manual">("pacs");

  // Bulk select — keyed by orthanc_id (unique per row; study_instance_uid is only
  // needed for the ingest call, resolved from `filtered` when acting on it).
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkIngesting, setBulkIngesting] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [bulkMessage, setBulkMessage] = useState<string | null>(null);

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

  const allFilteredSelected = filtered.length > 0 && filtered.every((s) => selected.has(s.orthanc_id));

  function toggleSelected(orthancId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(orthancId)) next.delete(orthancId);
      else next.add(orthancId);
      return next;
    });
  }

  function toggleSelectAll() {
    setSelected((prev) => {
      if (allFilteredSelected) {
        const next = new Set(prev);
        for (const s of filtered) next.delete(s.orthanc_id);
        return next;
      }
      const next = new Set(prev);
      for (const s of filtered) next.add(s.orthanc_id);
      return next;
    });
  }

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

  // Sequential, not Promise.all — mirrors the worklist's bulk actions so one
  // slow/failing study doesn't fire a burst of parallel requests, and each
  // row's own state still updates live as its call resolves.
  async function handleBulkIngest() {
    if (bulkIngesting) return;
    const targets = filtered.filter((s) => selected.has(s.orthanc_id) && !alreadyInPlatform(s.study_instance_uid));
    if (!targets.length) {
      setBulkMessage("All selected studies are already in the platform");
      setTimeout(() => setBulkMessage(null), 5000);
      return;
    }
    setBulkIngesting(true);
    setBulkMessage(null);
    let ok = 0, fail = 0;
    for (const s of targets) {
      try {
        await api.studies.ingest(s.study_instance_uid);
        setIngestedUids((prev) => [...prev, s.study_instance_uid]);
        ok++;
      } catch {
        fail++;
      }
    }
    await mutate();
    setSelected(new Set());
    setBulkIngesting(false);
    setBulkMessage(
      fail > 0 ? `Ingested ${ok} of ${targets.length} studies — ${fail} failed` : `Ingested ${ok} stud${ok === 1 ? "y" : "ies"}`
    );
    setTimeout(() => setBulkMessage(null), 5000);
  }

  async function handleBulkDelete() {
    const ids = Array.from(selected);
    setConfirmBulkDelete(false);
    if (!ids.length) return;
    setBulkDeleting(true);
    let ok = 0, fail = 0;
    for (const id of ids) {
      try {
        await api.orthanc.deleteStudy(id);
        ok++;
      } catch {
        fail++;
      }
    }
    await mutate();
    setSelected(new Set());
    setBulkDeleting(false);
    setBulkMessage(
      fail > 0 ? `Deleted ${ok} of ${ids.length} studies from Orthanc — ${fail} failed` : `Deleted ${ok} stud${ok === 1 ? "y" : "ies"} from Orthanc`
    );
    setTimeout(() => setBulkMessage(null), 5000);
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
              <p className="text-xs text-gray-600 dark:text-gray-400 dark:text-gray-500 leading-relaxed">Pick a source below, then click <strong>Ingest</strong> next to a study. The platform reads the DICOM metadata and registers the study so AI can analyse it.</p>
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

      {/* ── Source selector ──────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SourceCard
          icon={Upload}
          label="Upload from Computer"
          desc="Files or a whole folder from this device"
          meta="Drag & drop supported"
          active={activeSource === "local"}
          onClick={() => setActiveSource("local")}
        />
        <SourceCard
          icon={Database}
          label="Orthanc PACS"
          desc="Browse studies already received on-site"
          meta={orthancStudies ? `${orthancStudies.length} available` : undefined}
          active={activeSource === "pacs"}
          onClick={() => setActiveSource("pacs")}
        />
        <SourceCard
          icon={Network}
          label="DICOM Network"
          desc="Point your scanner or modality at this AE"
          meta="AE ORTHANC · Port 4242"
          active={activeSource === "network"}
          onClick={() => setActiveSource("network")}
        />
        <SourceCard
          icon={Hash}
          label="Manual Study UID"
          desc="Ingest a study you already know the UID for"
          meta="Advanced"
          active={activeSource === "manual"}
          onClick={() => setActiveSource("manual")}
        />
      </div>

      {/* ── Upload from this computer ─────────────────────────────────────── */}
      {activeSource === "local" && <LocalUploadPanel onIngested={() => { mutate(); }} />}

      {/* ── DICOM send config ─────────────────────────────────────────────── */}
      {activeSource === "network" && (
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
          After the scanner sends a study and Orthanc marks it "stable", it appears in the Orthanc PACS source automatically. Switch there and hit <strong>Refresh</strong> if you don't see it yet.
        </p>
      </div>
      )}

      {/* ── Orthanc PACS Browser ──────────────────────────────────────────── */}
      {activeSource === "pacs" && (
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
                  <th className="w-10 px-5 py-2.5">
                    <input
                      type="checkbox"
                      checked={allFilteredSelected}
                      onChange={toggleSelectAll}
                      aria-label="Select all studies"
                      className="accent-primary-600 cursor-pointer"
                    />
                  </th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Patient</th>
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
                  const isSelected = selected.has(s.orthanc_id);

                  return (
                    <tr
                      key={s.orthanc_id}
                      className={`transition-colors ${isSelected ? "bg-accent/5" : inPlatform ? "bg-green-50/40 dark:bg-green-950/20" : "hover:bg-gray-50 dark:hover:bg-white/5"}`}
                    >
                      <td className="px-5 py-3">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleSelected(s.orthanc_id)}
                          aria-label={`Select ${formatPatientName(s.patient_name)}`}
                          className="accent-primary-600 cursor-pointer"
                        />
                      </td>
                      <td className="px-3 py-3">
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
      )}

      {/* Bulk action bar — appears once studies are selected in the Orthanc browser */}
      {activeSource === "pacs" && selected.size > 0 && (
        <div className="sticky bottom-3 z-20 glass-raised rounded-2xl shadow-glow px-4 py-3 flex items-center gap-3 flex-wrap">
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary-600 text-white text-sm font-semibold">
            {selected.size} selected
          </span>
          <button
            onClick={() => setSelected(new Set())}
            className="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
          >
            Clear
          </button>
          {bulkMessage && (
            <span className="text-sm text-gray-500 dark:text-gray-400">{bulkMessage}</span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={handleBulkIngest}
              disabled={bulkIngesting || bulkDeleting}
              className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors whitespace-nowrap"
            >
              {bulkIngesting ? (
                <RefreshCw className="w-4 h-4 animate-spin motion-reduce:animate-none" />
              ) : (
                <Upload className="w-4 h-4" />
              )}
              Ingest selected
            </button>
            <button
              onClick={() => setConfirmBulkDelete(true)}
              disabled={bulkIngesting || bulkDeleting}
              className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-medium text-red-600 dark:text-red-400 border border-red-200 dark:border-red-800 rounded-lg hover:bg-red-50 dark:hover:bg-red-950 disabled:opacity-50 transition-colors whitespace-nowrap"
            >
              <Trash2 className="w-4 h-4" />
              Delete from Orthanc
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        tier="modal"
        danger
        open={confirmBulkDelete}
        title="Delete Studies from Orthanc"
        consequence={`${selected.size} stud${selected.size === 1 ? "y" : "ies"} will be permanently deleted from Orthanc PACS. The original DICOM images cannot be recovered unless the scanner resends the study${
          filtered.some((s) => selected.has(s.orthanc_id) && alreadyInPlatform(s.study_instance_uid))
            ? ". Any of these already in the platform will keep their AI results, but the viewer will no longer be able to load images."
            : "."
        }`}
        confirmLabel={bulkDeleting ? "Deleting…" : "Delete"}
        onConfirm={handleBulkDelete}
        onCancel={() => setConfirmBulkDelete(false)}
      />

      {/* ── Manual Study UID ─────────────────────────────────────────────── */}
      {activeSource === "manual" && (
      <div className="bg-white dark:bg-surface rounded-xl border border-gray-200 dark:border-gray-700 px-5 py-4">
        <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Ingest by Study Instance UID</p>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5 mb-3">
          Use this if the study doesn't appear in Orthanc PACS (e.g. sent via a worklist or external PACS).
        </p>
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
  );
}
