"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import { api, Study, Job, Result, CptSuggestion, ProtocolCheckResult, ComparisonData, MedGemmaDebug } from "@/lib/api";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useAuth } from "@/lib/auth";
import { ReportView } from "@/components/ReportView";
import { FusedViewer } from "@/components/FusedViewer";
import { ComparePanel } from "@/components/ComparePanel";
import { formatDate, formatPatientName } from "@/lib/format";
import { isCtReportUsecase } from "@/lib/ctReport";
import Link from "next/link";
import {
  ArrowLeft, ExternalLink, ArrowLeftRight, FileDown, Share2, AlertTriangle,
  CheckCircle, DollarSign, Stethoscope, ChevronDown, ChevronUp, Link2, X, TrendingUp, FileText, Bug, Truck
} from "lucide-react";

// ── Sequence type detector ────────────────────────────────────────────────────

type SeqBadge = { label: string; cls: string; type: string };

function getSeqBadge(desc: string | null, protocol: string | null): SeqBadge | null {
  const d = (desc ?? protocol ?? "").toLowerCase();
  if (!d) return null;

  // Setup / non-diagnostic
  if (/\bloc\b|localiz|scout/.test(d))           return { label: "LOC",    cls: "bg-gray-100 text-gray-500",    type: "setup" };
  if (/shim/.test(d))                             return { label: "SHIM",   cls: "bg-gray-100 text-gray-400",    type: "setup" };
  if (/survey/.test(d))                           return { label: "SURVEY", cls: "bg-gray-100 text-gray-400",    type: "setup" };

  // Anatomical sequences
  if (/flair|dark.?fluid/.test(d))                return { label: "FLAIR",  cls: "bg-purple-100 text-purple-700", type: "flair" };
  if (/\bt2\b|t2w|fse|tse/.test(d))              return { label: "T2",     cls: "bg-green-100 text-green-700",   type: "t2" };
  if (/\bt1\b|t1w|mprage|bravo|spgr|vibe|flash/.test(d))
                                                  return { label: "T1",     cls: "bg-blue-100 text-blue-700",     type: "t1" };

  // Functional / advanced
  if (/cistern|ssfp|fiesta|trufi|bssfp/.test(d)) return { label: "SSFP",   cls: "bg-indigo-100 text-indigo-700", type: "ssfp" };
  if (/dwi|diffusion|adc|dti|ivim/.test(d))      return { label: "DWI",    cls: "bg-orange-100 text-orange-700", type: "dwi" };
  if (/\bmap\b|adc/.test(d))                     return { label: "MAP",    cls: "bg-yellow-100 text-yellow-700", type: "map" };
  if (/swi|suscept|gre/.test(d))                 return { label: "SWI",    cls: "bg-red-100 text-red-700",       type: "swi" };
  if (/mra|angio/.test(d))                       return { label: "MRA",    cls: "bg-pink-100 text-pink-700",     type: "mra" };
  if (/perf|dsc|dce|asl/.test(d))               return { label: "PERF",   cls: "bg-rose-100 text-rose-700",     type: "perf" };
  if (/spec|mrsi|mrs/.test(d))                   return { label: "SPEC",   cls: "bg-teal-100 text-teal-700",     type: "spec" };

  // PET/CT
  if (/\bct\b|attenuat|transmi/.test(d))         return { label: "CT",     cls: "bg-slate-100 text-slate-700",   type: "ct" };
  if (/\bpt\b|\bpet\b|emission/.test(d))         return { label: "PET",    cls: "bg-amber-100 text-amber-700",   type: "pet" };

  return null;
}

// ─────────────────────────────────────────────────────────────────────────────

export default function StudyPage() {
  const params = useParams();
  const uid = params.uid as string;
  const { user: currentUser } = useAuth();

  const [study, setStudy] = useState<Study | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [results, setResults] = useState<Result[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedUsecase, setSelectedUsecase] = useState<string | null>(null);
  const [uiSchema, setUiSchema] = useState<any>(null);
  const [selectedResult, setSelectedResult] = useState<Result | null>(null);
  const [versions, setVersions] = useState<Result[]>([]);

  // Feature panels
  const [cptSuggestions, setCptSuggestions] = useState<CptSuggestion[] | null>(null);
  const [cptLoading, setCptLoading] = useState(false);
  const [cptOpen, setCptOpen] = useState(false);

  const [protocolCheck, setProtocolCheck] = useState<ProtocolCheckResult | null>(null);
  const [protocolLoading, setProtocolLoading] = useState(false);
  const [protocolOpen, setProtocolOpen] = useState(false);

  const [priorComparison, setPriorComparison] = useState<ComparisonData | null>(null);
  const [priorLoading, setPriorLoading] = useState(false);
  const [priorOpen, setPriorOpen] = useState(false);

  const [shareLoading, setShareLoading] = useState(false);
  const [shareLink, setShareLink] = useState<string | null>(null);
  const [shareCopied, setShareCopied] = useState(false);

  // MedGemma debug panel
  const [mgDebug, setMgDebug] = useState<MedGemmaDebug | null>(null);
  const [mgLoading, setMgLoading] = useState(false);
  const [mgRunning, setMgRunning] = useState(false);
  const [mgOpen, setMgOpen] = useState(false);

  const [pdfLoading, setPdfLoading] = useState(false);

  const [readingBusy, setReadingBusy] = useState(false);
  const [readingError, setReadingError] = useState<string | null>(null);
  const [pendingSign, setPendingSign] = useState(false);
  const runReading = async (fn: () => Promise<unknown>) => {
    setReadingBusy(true);
    setReadingError(null);
    try {
      await fn();
      const s = await api.studies.get(uid);
      setStudy(s);
    } catch (e: any) {
      setReadingError(e.message || "Action failed");
    } finally {
      setReadingBusy(false);
    }
  };

  useEffect(() => {
    async function load() {
      try {
        const [s, j, r] = await Promise.all([
          api.studies.get(uid),
          api.jobs.listByStudy(uid).then((d) => d.jobs),
          api.results.listByStudy(uid).then((d) => d.results).catch(() => [] as Result[]),
        ]);
        setStudy(s);
        setJobs(j);
        setResults(r);
        if (r.length > 0 && !selectedUsecase) setSelectedUsecase(r[0].usecase_name);
      } catch (e) {
        console.error(e);
      } finally {
        setLoading(false);
      }
    }
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, [uid]);

  // Track the selected use case's result across the 10 s poll. Keyed on `results`
  // so a freshly-arrived result is picked up — but deliberately does NOT reset the
  // feature panels (that lives in the effect below), or every poll would close an
  // open CPT / Protocol / Prior / MedGemma panel mid-view.
  useEffect(() => {
    if (!selectedUsecase) {
      setSelectedResult(null);
      return;
    }
    const r = results.find((res) => res.usecase_name === selectedUsecase);
    setSelectedResult(r || null);
  }, [selectedUsecase, results]);

  // Per-use-case setup + panel reset. Keyed ONLY on the use case (and uid), NOT on
  // `results`, so it fires when the user switches use case — not on every poll.
  useEffect(() => {
    if (!selectedUsecase) return;
    api.usecases.getUiSchema(selectedUsecase).then(setUiSchema).catch(() => setUiSchema(null));
    api.results.listVersions(uid, selectedUsecase).then((d) => setVersions(d.results)).catch(() => setVersions([]));
    setCptSuggestions(null);
    setCptOpen(false);
    setProtocolCheck(null);
    setProtocolOpen(false);
    setPriorComparison(null);
    setPriorOpen(false);
    setShareLink(null);
    setMgDebug(null);
    setMgOpen(false);
  }, [selectedUsecase, uid]);

  const loadCptSuggestions = useCallback(async () => {
    if (!selectedUsecase || cptLoading) return;
    setCptLoading(true);
    try {
      const data = await api.cpt.getSuggestions(uid, selectedUsecase);
      setCptSuggestions(data.suggestions);
      setCptOpen(true);
    } catch (e: any) {
      alert("CPT suggestions not available: " + e.message);
    } finally {
      setCptLoading(false);
    }
  }, [uid, selectedUsecase, cptLoading]);

  const loadProtocolCheck = useCallback(async () => {
    if (!selectedUsecase || protocolLoading) return;
    setProtocolLoading(true);
    try {
      const data = await api.protocol.check(uid, selectedUsecase);
      setProtocolCheck(data);
      setProtocolOpen(true);
    } catch (e: any) {
      alert("Protocol check not available: " + e.message);
    } finally {
      setProtocolLoading(false);
    }
  }, [uid, selectedUsecase, protocolLoading]);

  const loadPriorComparison = useCallback(async () => {
    if (!selectedUsecase || priorLoading) return;
    setPriorLoading(true);
    try {
      const data = await api.priorComparison.get(uid, selectedUsecase);
      setPriorComparison(data);
      setPriorOpen(true);
    } catch (e: any) {
      alert("No prior study found for comparison: " + e.message);
    } finally {
      setPriorLoading(false);
    }
  }, [uid, selectedUsecase, priorLoading]);

  // Fetch MedGemma inputs (prompt + all images) without calling the model — fast.
  const loadMedgemmaDebug = useCallback(async () => {
    if (!selectedUsecase || mgLoading) return;
    setMgLoading(true);
    try {
      const data = await api.medgemmaDebug.get(selectedUsecase, uid, {
        run: false,
        includeImages: true,
      });
      setMgDebug(data);
      setMgOpen(true);
    } catch (e: any) {
      alert("MedGemma debug not available: " + e.message);
    } finally {
      setMgLoading(false);
    }
  }, [uid, selectedUsecase, mgLoading]);

  // Re-run the local model. sendAll=true feeds every image (incl. Stage-3 crops).
  const runMedgemma = useCallback(
    async (sendAll: boolean) => {
      if (!selectedUsecase || mgRunning) return;
      setMgRunning(true);
      try {
        const data = await api.medgemmaDebug.get(selectedUsecase, uid, {
          run: true,
          sendAll,
          includeImages: true,
        });
        setMgDebug(data);
        setMgOpen(true);
      } catch (e: any) {
        alert("MedGemma run failed: " + e.message);
      } finally {
        setMgRunning(false);
      }
    },
    [uid, selectedUsecase, mgRunning]
  );

  const handleDownloadPdf = useCallback(async () => {
    if (!selectedResult || pdfLoading) return;
    setPdfLoading(true);
    try {
      const blob = await api.pdf.downloadBlob(selectedResult.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `report_${selectedResult.id.slice(0, 8)}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: any) {
      alert("PDF generation failed: " + e.message);
    } finally {
      setPdfLoading(false);
    }
  }, [selectedResult, pdfLoading]);

  const handleCreateShareLink = useCallback(async () => {
    if (!selectedResult || shareLoading) return;
    setShareLoading(true);
    try {
      const link = await api.portal.createShareLink(selectedResult.id, currentUser?.username || "unknown", 7);
      const portalUrl = `${window.location.origin}/portal/${link.token}`;
      setShareLink(portalUrl);
    } catch (e: any) {
      alert("Share link creation failed: " + e.message);
    } finally {
      setShareLoading(false);
    }
  }, [selectedResult, shareLoading]);

  const copyShareLink = () => {
    if (!shareLink) return;
    navigator.clipboard.writeText(shareLink).then(() => {
      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 2000);
    });
  };

  if (loading) return <p className="text-gray-500 dark:text-gray-400 dark:text-gray-500 p-4">Loading study...</p>;
  if (!study) return <p className="text-red-500 p-4">Study not found</p>;

  // All studies open in the OHIF v3 / Cornerstone3D viewer; the mode is chosen
  // by content:
  //  • PET/CT (study actually contains a PT series) → OHIF TMTV mode
  //    ("/ohif/tmtv"): its hanging protocol auto-lays out CT, PET, and a fused
  //    PET-on-CT viewport (+ rotating PET MIP), so the study opens already fused.
  //  • Everything else → OHIF longitudinal viewer ("/ohif/viewer"), which
  //    surfaces MPR, annotations, prior-study comparison, hanging protocols and
  //    the full manipulation toolset (vs. the lighter Orthanc Stone viewer used
  //    previously).
  // PET is detected from actual PT series, never the usecase_name — a pet_ct
  // pipeline can be mis-routed onto non-PET data, where TMTV would show nothing.
  const hasPetSeries = study.series.some((s) => s.modality === "PT");
  const viewerUrl = hasPetSeries
    ? `/ohif/tmtv?StudyInstanceUIDs=${uid}`
    : `/ohif/viewer?StudyInstanceUIDs=${uid}`;

  // The lean native fused PET/CT viewer renders from the stored SUV + CT
  // artifacts, so it's only available once a pet_ct result exists. Until then
  // (or for the full interactive experience), the OHIF TMTV mode is used.
  const petResult = results.find((r) => r.usecase_name.startsWith("pet_ct"));
  const showNativeFused = hasPetSeries && !!petResult;

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <Link href="/worklist" className="inline-flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">
        <ArrowLeft className="w-4 h-4" /> Back to Worklist
      </Link>

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {formatPatientName(study.patient_name)}
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500 mt-0.5">
            {study.study_description || "No description"} &middot;{" "}
            {study.body_part_examined || study.modality || ""} &middot;{" "}
            {formatDate(study.study_date)}
          </p>
        </div>
        <a
          href={viewerUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          <ExternalLink className="w-4 h-4" />
          {hasPetSeries ? "Open in OHIF" : "Open Viewer"}
        </a>
      </div>

      {/* Reading workflow */}
      {(() => {
        const rs = study.reading_status || "unread";
        const btn = "px-3 py-1.5 text-xs font-medium rounded-lg transition-colors disabled:opacity-50";
        return (
          <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 px-4 py-3">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">Reading</span>
                <StatusBadge variant="reading" status={rs} />
                {study.assigned_to_username && (
                  <span className="text-sm text-gray-600 dark:text-gray-400 dark:text-gray-500">
                    Assigned to <span className="font-medium">{study.assigned_to_username}</span>
                  </span>
                )}
                {study.tat_signoff_minutes != null ? (
                  <span className="text-xs text-gray-400 dark:text-gray-500">Turnaround: {study.tat_signoff_minutes} min</span>
                ) : study.tat_report_minutes != null ? (
                  <span className="text-xs text-gray-400 dark:text-gray-500">Report TAT: {study.tat_report_minutes} min</span>
                ) : null}
              </div>
              <div className="flex items-center gap-2">
                {rs === "unread" && (
                  <>
                    <button disabled={readingBusy} onClick={() => runReading(() => api.reading.claim(uid))}
                      className={`${btn} text-white bg-primary-600 hover:bg-primary-700`}>Claim</button>
                    <button disabled={readingBusy} onClick={() => runReading(() => api.reading.autoAssign(uid))}
                      className={`${btn} text-primary-700 border border-primary-200 hover:bg-primary-50`}>Auto-assign</button>
                  </>
                )}
                {rs === "in_progress" && (
                  <>
                    <button disabled={readingBusy} onClick={() => runReading(() => api.reading.report(uid))}
                      className={`${btn} text-white bg-amber-600 hover:bg-amber-700`}>Mark Reported</button>
                    <button disabled={readingBusy} onClick={() => runReading(() => api.reading.unclaim(uid))}
                      className={`${btn} text-gray-600 dark:text-gray-400 dark:text-gray-500 border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-surface-raised`}>Release</button>
                  </>
                )}
                {rs === "reported" && (
                  <button disabled={readingBusy} onClick={() => setPendingSign(true)}
                    className={`${btn} text-white bg-green-600 hover:bg-green-700`}>Sign Off</button>
                )}
                {rs === "signed" && (
                  <span className="text-xs text-green-600 dark:text-green-400 flex items-center gap-1">
                    <CheckCircle className="w-4 h-4" /> Signed
                  </span>
                )}
              </div>
            </div>
            {readingError && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{readingError}</p>}
            <ConfirmDialog
              tier="modal"
              open={pendingSign}
              title="Sign Off Report"
              consequence="Signing off finalizes this study's reading status as complete and is recorded against your account. This action cannot be undone from this screen."
              confirmLabel="Sign Off"
              onConfirm={async () => {
                await runReading(() => api.reading.sign(uid));
                setPendingSign(false);
              }}
              onCancel={() => setPendingSign(false)}
            />
          </div>
        );
      })()}

      {/* Info Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
          <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-3">Study Information</h2>
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between">
              <dt className="text-gray-500 dark:text-gray-400 dark:text-gray-500">Patient ID</dt>
              <dd className="font-medium text-gray-900 dark:text-gray-100">{study.patient_id || "-"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-gray-500 dark:text-gray-400 dark:text-gray-500">Accession #</dt>
              <dd className="font-medium text-gray-900 dark:text-gray-100">{study.accession_number || "-"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-gray-500 dark:text-gray-400 dark:text-gray-500">Institution</dt>
              <dd className="font-medium text-gray-900 dark:text-gray-100">{study.institution_name || "-"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-gray-500 dark:text-gray-400 dark:text-gray-500">Series Count</dt>
              <dd className="font-medium text-gray-900 dark:text-gray-100">{study.series.length}</dd>
            </div>
          </dl>
        </div>

        <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
          <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-3">Series</h2>
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {study.series
              .slice()
              .sort((a, b) => (a.series_number ?? 999) - (b.series_number ?? 999))
              .map((s) => {
                const badge = getSeqBadge(s.series_description, s.protocol_name);
                const isSetup = badge?.type === "setup";
                return (
                  <div
                    key={s.series_instance_uid}
                    className={`text-sm border-b border-gray-50 pb-2 ${isSetup ? "opacity-50" : ""}`}
                  >
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {badge && (
                        <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide shrink-0 ${badge.cls}`}>
                          {badge.label}
                        </span>
                      )}
                      <span className={`font-medium truncate ${isSetup ? "text-gray-500 dark:text-gray-400 dark:text-gray-500" : "text-gray-900 dark:text-gray-100"}`}>
                        {s.series_description || s.protocol_name || `Series ${s.series_number}`}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                      <span>{s.modality}</span>
                      <span>&middot;</span>
                      <span>{s.num_instances} slices</span>
                      {s.slice_thickness && (
                        <>
                          <span>&middot;</span>
                          <span>{s.slice_thickness.toFixed(1)} mm</span>
                        </>
                      )}
                      {s.protocol_name && s.protocol_name !== s.series_description && (
                        <>
                          <span>&middot;</span>
                          <span className="italic truncate max-w-[100px]">{s.protocol_name}</span>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>
          {/* Legend */}
          <div className="mt-2 pt-2 border-t border-gray-50 flex flex-wrap gap-1.5">
            {Array.from(
              new Map(
                study.series
                  .map((s) => getSeqBadge(s.series_description, s.protocol_name))
                  .filter((b): b is NonNullable<typeof b> => b !== null)
                  .map((b) => [b.label, b])
              ).values()
            ).map((b) => (
              <span key={b.label} className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold uppercase ${b.cls}`}>
                {b.label}
              </span>
            ))}
          </div>
        </div>

        <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
          <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-3">AI Jobs</h2>
          {jobs.length === 0 ? (
            <p className="text-sm text-gray-400 dark:text-gray-500">No jobs run yet</p>
          ) : (
            <div className="space-y-2">
              {jobs.map((job) => (
                <div key={job.id} className="border border-gray-100 dark:border-gray-800 rounded-lg p-2.5">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                      {job.usecase_name.replace(/_/g, " ")}
                    </span>
                    <StatusBadge variant="job" status={job.status} />
                  </div>
                  {job.status_message && <p className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 mt-1">{job.status_message}</p>}
                  {job.progress > 0 && job.status !== "completed" && (
                    <div className="mt-2 bg-gray-100 dark:bg-gray-800 rounded-full h-1.5">
                      <div
                        className="bg-primary-600 h-1.5 rounded-full transition-all"
                        style={{ width: `${job.progress * 100}%` }}
                      />
                    </div>
                  )}
                  {job.error_detail && <p className="text-xs text-red-500 mt-1 truncate">{job.error_detail}</p>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* DICOM Viewer */}
      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider">DICOM Viewer</h2>
            {showNativeFused ? (
              <span className="text-xs bg-amber-100 text-amber-700 dark:text-amber-300 px-2 py-0.5 rounded-full font-medium">
                CT + PET
              </span>
            ) : hasPetSeries ? (
              <span className="text-xs bg-orange-100 text-orange-700 px-2 py-0.5 rounded-full font-medium">
                OHIF — PET/CT Fusion
              </span>
            ) : null}
          </div>
          <a
            href={viewerUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-primary-600 hover:text-primary-700 flex items-center gap-1"
          >
            {hasPetSeries ? "Open full viewer (OHIF)" : "Open in new tab"}{" "}
            <ExternalLink className="w-3 h-3" />
          </a>
        </div>
        {showNativeFused && petResult ? (
          <FusedViewer studyUid={uid} usecase={petResult.usecase_name} />
        ) : (
          <iframe
            src={viewerUrl}
            className="w-full border-0"
            style={{ height: hasPetSeries ? "700px" : "500px" }}
            title={hasPetSeries ? "OHIF PET/CT Viewer" : "DICOM Viewer"}
            allow="fullscreen"
          />
        )}
      </div>

      {/* AI Results - Tabs + Actions */}
      {results.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            {results.length > 1 &&
              results.map((r) => (
                <button
                  key={r.usecase_name}
                  onClick={() => setSelectedUsecase(r.usecase_name)}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                    selectedUsecase === r.usecase_name
                      ? "bg-primary-600 text-white"
                      : "bg-white dark:bg-surface text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-surface-raised"
                  }`}
                >
                  {r.usecase_name.replace(/_/g, " ").replace(/\b\w/g, (c: string) => c.toUpperCase())}
                </button>
              ))}

            <div className="ml-auto flex items-center gap-2 flex-wrap">
              {versions.length >= 2 && selectedResult && (
                <Link
                  href={`/compare?a=${versions[1].id}&b=${versions[0].id}`}
                  className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-primary-600 border border-primary-200 rounded-lg hover:bg-primary-50 transition-colors"
                >
                  <ArrowLeftRight className="w-4 h-4" /> Compare versions
                </Link>
              )}
              {selectedResult && (
                <>
                  <button
                    onClick={loadCptSuggestions}
                    disabled={cptLoading}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-emerald-700 border border-emerald-200 rounded-lg hover:bg-emerald-50 transition-colors disabled:opacity-50"
                  >
                    <DollarSign className="w-4 h-4" />
                    {cptLoading ? "Loading..." : "CPT Codes"}
                  </button>
                  <button
                    onClick={loadProtocolCheck}
                    disabled={protocolLoading}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-violet-700 border border-violet-200 rounded-lg hover:bg-violet-50 transition-colors disabled:opacity-50"
                  >
                    <Stethoscope className="w-4 h-4" />
                    {protocolLoading ? "Checking..." : "Protocol Check"}
                  </button>
                  <button
                    onClick={loadPriorComparison}
                    disabled={priorLoading}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-amber-700 dark:text-amber-300 border border-amber-200 rounded-lg hover:bg-amber-50 dark:hover:bg-amber-950 transition-colors disabled:opacity-50"
                  >
                    <ArrowLeftRight className="w-4 h-4" />
                    {priorLoading ? "Loading..." : "Prior Comparison"}
                  </button>
                  {["pet_ct", "pet_ct_brain"].includes(selectedResult.usecase_name) && (
                    <Link
                      href={`/study/${uid}/report/${selectedResult.usecase_name}`}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-indigo-700 border border-indigo-200 rounded-lg hover:bg-indigo-50 transition-colors"
                    >
                      <FileText className="w-4 h-4" /> PET-CT Report
                    </Link>
                  )}
                  {selectedResult.usecase_name === "pet_ct" && (
                    <button
                      onClick={loadMedgemmaDebug}
                      disabled={mgLoading}
                      title="Inspect the exact prompt, images, and raw output sent to the local MedGemma model"
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-fuchsia-700 border border-fuchsia-200 rounded-lg hover:bg-fuchsia-50 transition-colors disabled:opacity-50"
                    >
                      <Bug className="w-4 h-4" />
                      {mgLoading ? "Loading..." : "MedGemma Debug"}
                    </button>
                  )}
                  {selectedResult.usecase_name === "mammography" && (
                    <Link
                      href={`/study/${uid}/report/mammography`}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-rose-700 border border-rose-200 rounded-lg hover:bg-rose-50 transition-colors"
                    >
                      <FileText className="w-4 h-4" /> Mammography Report
                    </Link>
                  )}
                  {isCtReportUsecase(selectedResult.usecase_name) && (
                    <Link
                      href={`/study/${uid}/report/${selectedResult.usecase_name}`}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-cyan-700 border border-cyan-200 rounded-lg hover:bg-cyan-50 transition-colors"
                    >
                      <FileText className="w-4 h-4" /> AI Report
                    </Link>
                  )}
                  <button
                    onClick={handleDownloadPdf}
                    disabled={pdfLoading}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-surface-raised transition-colors disabled:opacity-50"
                  >
                    <FileDown className="w-4 h-4" />
                    {pdfLoading ? "Generating..." : "Download PDF"}
                  </button>
                  <button
                    onClick={handleCreateShareLink}
                    disabled={shareLoading}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-blue-700 dark:text-blue-300 border border-blue-200 rounded-lg hover:bg-blue-50 dark:hover:bg-blue-950 transition-colors disabled:opacity-50"
                  >
                    <Share2 className="w-4 h-4" />
                    {shareLoading ? "Creating..." : "Share"}
                  </button>
                  <Link
                    href={`/study/${uid}/delivery`}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-surface-raised transition-colors"
                  >
                    <Truck className="w-4 h-4" /> Delivery Status
                  </Link>
                  {study.patient_id && (
                    <Link
                      href={`/admin/patients/${encodeURIComponent(study.patient_id)}/trend/${selectedUsecase}`}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-purple-700 border border-purple-200 rounded-lg hover:bg-purple-50 transition-colors"
                    >
                      <TrendingUp className="w-4 h-4" /> Trend
                    </Link>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Share link display */}
          {shareLink && (
            <div className="mb-4 bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg p-4 flex items-center gap-3">
              <Link2 className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-blue-700 dark:text-blue-300 mb-1">Referring Physician Portal Link (expires in 7 days)</p>
                <p className="text-sm text-blue-800 font-mono truncate">{shareLink}</p>
              </div>
              <button
                onClick={copyShareLink}
                className="px-3 py-1.5 text-xs font-medium text-blue-700 dark:text-blue-300 bg-blue-100 hover:bg-blue-200 rounded-lg transition-colors shrink-0"
              >
                {shareCopied ? "Copied!" : "Copy"}
              </button>
              <button onClick={() => setShareLink(null)} className="text-blue-400 hover:text-blue-600 dark:hover:text-blue-400">
                <X className="w-4 h-4" />
              </button>
            </div>
          )}

          {/* CPT Suggestions Panel */}
          {cptSuggestions && (
            <div className="mb-4 bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
              <button
                onClick={() => setCptOpen(!cptOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-surface-raised transition-colors"
              >
                <div className="flex items-center gap-2">
                  <DollarSign className="w-4 h-4 text-emerald-600" />
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">CPT Billing Code Suggestions</span>
                  <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full font-medium">
                    {cptSuggestions.length} codes
                  </span>
                </div>
                {cptOpen ? <ChevronUp className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
              </button>
              {cptOpen && (
                <div className="p-4">
                  <div className="space-y-3">
                    {cptSuggestions.map((cpt, i) => (
                      <div
                        key={cpt.code}
                        className={`flex items-start gap-3 p-3 rounded-lg border ${
                          cpt.category === "addon" ? "border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-surface-raised" : "border-emerald-100 bg-emerald-50"
                        }`}
                      >
                        <div className="shrink-0">
                          <span className={`text-sm font-bold font-mono ${cpt.category === "addon" ? "text-gray-600 dark:text-gray-400 dark:text-gray-500" : "text-emerald-700"}`}>
                            {cpt.code}
                          </span>
                          {cpt.category === "primary" && i === 0 && (
                            <span className="ml-2 text-xs bg-emerald-600 text-white px-1.5 py-0.5 rounded">Primary</span>
                          )}
                          {cpt.category === "addon" && (
                            <span className="ml-2 text-xs bg-gray-400 text-white px-1.5 py-0.5 rounded">Add-on</span>
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{cpt.description}</p>
                        </div>
                        <div className="shrink-0 text-right">
                          <span className="text-xs font-medium text-gray-600 dark:text-gray-400 dark:text-gray-500">
                            {(cpt.confidence * 100).toFixed(0)}% confidence
                          </span>
                          <div className="w-16 bg-gray-200 rounded-full h-1 mt-1">
                            <div
                              className="bg-emerald-500 h-1 rounded-full"
                              style={{ width: `${cpt.confidence * 100}%` }}
                            />
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-3">
                    AI-generated suggestions only. Verify against clinical documentation before billing.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Protocol Check Panel */}
          {protocolCheck && (
            <div className="mb-4 bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
              <button
                onClick={() => setProtocolOpen(!protocolOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-surface-raised transition-colors"
              >
                <div className="flex items-center gap-2">
                  {protocolCheck.status === "ok" || protocolCheck.issues.length === 0 ? (
                    <CheckCircle className="w-4 h-4 text-green-500" />
                  ) : (
                    <AlertTriangle className="w-4 h-4 text-amber-500" />
                  )}
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">Protocol Check</span>
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      protocolCheck.status === "ok" || protocolCheck.issues.length === 0
                        ? "bg-green-100 text-green-700 dark:text-green-300"
                        : "bg-amber-100 text-amber-700 dark:text-amber-300"
                    }`}
                  >
                    {protocolCheck.status === "ok" || protocolCheck.issues.length === 0
                      ? "Passed"
                      : `${protocolCheck.issues.length} issue${protocolCheck.issues.length !== 1 ? "s" : ""}`}
                  </span>
                </div>
                {protocolOpen ? <ChevronUp className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
              </button>
              {protocolOpen && (
                <div className="p-4">
                  {protocolCheck.issues.length === 0 ? (
                    <p className="text-sm text-green-700 dark:text-green-300 flex items-center gap-2">
                      <CheckCircle className="w-4 h-4" /> All {protocolCheck.series_checked} series checked — no protocol issues found.
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {protocolCheck.issues.map((issue, i) => (
                        <div
                          key={i}
                          className={`p-3 rounded-lg border ${
                            issue.severity === "error"
                              ? "border-red-200 bg-red-50 dark:bg-red-950"
                              : issue.severity === "warning"
                              ? "border-amber-200 bg-amber-50 dark:bg-amber-950"
                              : "border-blue-100 bg-blue-50 dark:bg-blue-950"
                          }`}
                        >
                          <div className="flex items-center gap-2 mb-1">
                            <AlertTriangle
                              className={`w-3.5 h-3.5 ${
                                issue.severity === "error" ? "text-red-500" :
                                issue.severity === "warning" ? "text-amber-500" : "text-blue-400"
                              }`}
                            />
                            <span className="text-xs font-semibold text-gray-700 dark:text-gray-300">{issue.series_description}</span>
                            <span
                              className={`text-xs px-1.5 py-0.5 rounded font-medium uppercase ${
                                issue.severity === "error" ? "bg-red-200 text-red-700 dark:text-red-300" :
                                issue.severity === "warning" ? "bg-amber-200 text-amber-700 dark:text-amber-300" :
                                "bg-blue-100 text-blue-700 dark:text-blue-300"
                              }`}
                            >
                              {issue.severity}
                            </span>
                          </div>
                          <p className="text-sm text-gray-800 dark:text-gray-200">{issue.message}</p>
                          {issue.suggestion && (
                            <p className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 mt-1">
                              Suggestion: <span className="font-medium">{issue.suggestion}</span>
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Prior Comparison Panel */}
          {priorComparison && (
            <div className="mb-4 bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
              <button
                onClick={() => setPriorOpen(!priorOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-surface-raised transition-colors"
              >
                <div className="flex items-center gap-2">
                  <ArrowLeftRight className="w-4 h-4 text-amber-600" />
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">Prior Study Comparison</span>
                  {priorComparison.delta.days_between !== null && (
                    <span className="text-xs bg-amber-100 text-amber-700 dark:text-amber-300 px-2 py-0.5 rounded-full font-medium">
                      {priorComparison.delta.days_between} days ago
                    </span>
                  )}
                </div>
                {priorOpen ? <ChevronUp className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
              </button>
              {priorOpen && (
                <div className="p-4">
                  <ComparePanel data={priorComparison} />
                </div>
              )}
            </div>
          )}

          {/* MedGemma Debug Panel */}
          {mgDebug && (
            <div className="mb-4 bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
              <button
                onClick={() => setMgOpen(!mgOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-surface-raised transition-colors"
              >
                <div className="flex items-center gap-2 flex-wrap">
                  <Bug className="w-4 h-4 text-fuchsia-600" />
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">MedGemma Debug</span>
                  <span className="text-xs bg-fuchsia-100 text-fuchsia-700 px-2 py-0.5 rounded-full font-medium">
                    {mgDebug.medgemma.model}
                  </span>
                  <span className="text-xs text-gray-400 dark:text-gray-500">
                    {mgDebug.inputs.n_images_sent}/{mgDebug.inputs.n_images_total} images sent
                  </span>
                  {mgDebug.medgemma.ready === false && (
                    <span className="text-xs bg-red-100 text-red-700 dark:text-red-300 px-2 py-0.5 rounded-full font-medium">
                      Ollama unreachable
                    </span>
                  )}
                </div>
                {mgOpen ? <ChevronUp className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
              </button>
              {mgOpen && (
                <div className="p-4 space-y-4">
                  {/* Config + run controls */}
                  <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500">
                    <span>base_url: <span className="font-mono text-gray-700 dark:text-gray-300">{mgDebug.medgemma.base_url}</span></span>
                    <span>&middot; enabled: {String(mgDebug.medgemma.enabled)}</span>
                    {mgDebug.stored_ai_report_provider && (
                      <span>&middot; stored report by: <span className="font-mono text-gray-700 dark:text-gray-300">{mgDebug.stored_ai_report_provider}</span></span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      onClick={() => runMedgemma(false)}
                      disabled={mgRunning}
                      className="px-3 py-1.5 text-xs font-medium text-white bg-fuchsia-600 hover:bg-fuchsia-700 rounded-lg disabled:opacity-50"
                    >
                      {mgRunning ? "Running…" : "Run model (6 images)"}
                    </button>
                    <button
                      onClick={() => runMedgemma(true)}
                      disabled={mgRunning}
                      className="px-3 py-1.5 text-xs font-medium text-fuchsia-700 border border-fuchsia-200 hover:bg-fuchsia-50 rounded-lg disabled:opacity-50"
                    >
                      {mgRunning ? "Running…" : "Run send_all (every image)"}
                    </button>
                    <span className="text-xs text-gray-400 dark:text-gray-500">≈ 20–30 s per run</span>
                  </div>

                  {mgDebug.note && (
                    <p className="text-xs text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950 border border-amber-200 rounded p-2">{mgDebug.note}</p>
                  )}

                  {/* Images (inputs) */}
                  <div>
                    <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-2">
                      Images ({mgDebug.inputs.images.length}) — <span className="text-green-600 dark:text-green-400">sent</span> vs generated-but-not-sent
                    </p>
                    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
                      {mgDebug.inputs.images.map((im) => (
                        <div key={im.name} className="border border-gray-100 dark:border-gray-800 rounded-lg overflow-hidden">
                          {im.base64 ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={`data:image/png;base64,${im.base64}`}
                              alt={im.name}
                              className="w-full h-32 object-contain bg-black"
                            />
                          ) : (
                            <div className="w-full h-32 bg-gray-100 dark:bg-gray-800 flex items-center justify-center text-xs text-gray-400 dark:text-gray-500">
                              no preview
                            </div>
                          )}
                          <div className="p-1.5">
                            <div className="flex items-center justify-between gap-1">
                              <span className="text-[11px] font-medium text-gray-700 dark:text-gray-300 truncate" title={im.name}>{im.name}</span>
                              {im.sent_to_model ? (
                                <span className="text-[9px] font-bold uppercase bg-green-100 text-green-700 dark:text-green-300 px-1 py-0.5 rounded shrink-0">sent</span>
                              ) : (
                                <span className="text-[9px] font-bold uppercase bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 px-1 py-0.5 rounded shrink-0">not sent</span>
                              )}
                            </div>
                            <span className="text-[10px] text-gray-400 dark:text-gray-500">
                              {(im.bytes / 1024).toFixed(0)} KB · {im.artifact_type}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Prompt (input) */}
                  <div>
                    <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-2">
                      Prompt ({mgDebug.inputs.prompt_chars} chars)
                    </p>
                    <pre className="text-[11px] whitespace-pre-wrap bg-gray-50 dark:bg-surface-raised border border-gray-100 dark:border-gray-800 rounded p-3 max-h-80 overflow-auto text-gray-700 dark:text-gray-300">
                      {mgDebug.inputs.prompt}
                    </pre>
                  </div>

                  {/* Output */}
                  {mgDebug.output && (
                    <div>
                      <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-2">
                        Raw output · {mgDebug.output.elapsed_s}s ·{" "}
                        {mgDebug.output.parsed_ok ? (
                          <span className="text-green-600 dark:text-green-400">parsed ✓</span>
                        ) : (
                          <span className="text-red-600 dark:text-red-400">parse failed{mgDebug.output.parse_error ? ` (${mgDebug.output.parse_error})` : ""}</span>
                        )}
                      </p>
                      <pre className="text-[11px] whitespace-pre-wrap bg-gray-900 text-green-200 rounded p-3 max-h-80 overflow-auto">
                        {mgDebug.output.raw}
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Professional Report */}
          {selectedResult && uiSchema ? (
            <ReportView study={study} result={selectedResult} uiSchema={uiSchema} />
          ) : selectedUsecase ? (
            <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-8 text-center text-gray-400 dark:text-gray-500">
              Loading report...
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
