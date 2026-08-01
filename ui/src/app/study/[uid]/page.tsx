"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { api, Study, Job, Result, CptSuggestion, ProtocolCheckResult, ComparisonData, ClinicalForStudy } from "@/lib/api";
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
  CheckCircle, DollarSign, Stethoscope, ChevronDown, ChevronUp, Link2, X, TrendingUp, FileText, Truck,
  ArrowUpRight, MoreHorizontal, Hash, Building2, Layers, User, Clock,
} from "lucide-react";

// Modality → gradient, mirrored from the dashboard/worklist convention so a
// study's modality reads the same colour everywhere in the app.
const HERO_MODALITY_GRAD: Record<string, string> = {
  MR: "from-cyan-500 to-teal-600",
  CT: "from-indigo-500 to-blue-600",
  PT: "from-amber-500 to-orange-600",
  NM: "from-emerald-500 to-green-600",
  US: "from-teal-500 to-cyan-600",
  MG: "from-pink-500 to-rose-600",
};
function heroModalityGrad(m: string | null | undefined): string {
  return HERO_MODALITY_GRAD[(m || "").toUpperCase()] ?? "from-slate-500 to-slate-600";
}

// DICOM Age String (0010,1010) is zero-padded to 3 digits + a unit letter, e.g.
// "022Y" or "070Y" — strip the padding for a compact, easy-to-read "22Y"/"70Y".
// Falsy/unrecognized input (e.g. an onboarding age BAND like "40-64") is passed
// through unchanged.
function compactAge(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const m = String(raw).trim().match(/^0*(\d{1,3})([YMWD])$/i);
  return m ? `${m[1]}${m[2].toUpperCase()}` : raw;
}

// ── Sequence type detector ────────────────────────────────────────────────────

type SeqBadge = { label: string; cls: string; type: string };

function getSeqBadge(desc: string | null, protocol: string | null): SeqBadge | null {
  const d = (desc ?? protocol ?? "").toLowerCase();
  if (!d) return null;

  // Setup / non-diagnostic
  if (/\bloc\b|localiz|scout/.test(d))           return { label: "LOC",    cls: "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400",    type: "setup" };
  if (/shim/.test(d))                             return { label: "SHIM",   cls: "bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500",    type: "setup" };
  if (/survey/.test(d))                           return { label: "SURVEY", cls: "bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500",    type: "setup" };

  // Anatomical sequences
  if (/flair|dark.?fluid/.test(d))                return { label: "FLAIR",  cls: "bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-300", type: "flair" };
  if (/\bt2\b|t2w|fse|tse/.test(d))              return { label: "T2",     cls: "bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300",   type: "t2" };
  if (/\bt1\b|t1w|mprage|bravo|spgr|vibe|flash/.test(d))
                                                  return { label: "T1",     cls: "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300",     type: "t1" };

  // Functional / advanced
  if (/cistern|ssfp|fiesta|trufi|bssfp/.test(d)) return { label: "SSFP",   cls: "bg-indigo-100 dark:bg-indigo-900 text-indigo-700 dark:text-indigo-300", type: "ssfp" };
  if (/dwi|diffusion|adc|dti|ivim/.test(d))      return { label: "DWI",    cls: "bg-orange-100 dark:bg-orange-900 text-orange-700 dark:text-orange-300", type: "dwi" };
  if (/\bmap\b|adc/.test(d))                     return { label: "MAP",    cls: "bg-yellow-100 dark:bg-yellow-900 text-yellow-700 dark:text-yellow-300", type: "map" };
  if (/swi|suscept|gre/.test(d))                 return { label: "SWI",    cls: "bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300",       type: "swi" };
  if (/mra|angio/.test(d))                       return { label: "MRA",    cls: "bg-pink-100 text-pink-700",     type: "mra" };
  if (/perf|dsc|dce|asl/.test(d))               return { label: "PERF",   cls: "bg-rose-100 dark:bg-rose-900 text-rose-700 dark:text-rose-300",     type: "perf" };
  if (/spec|mrsi|mrs/.test(d))                   return { label: "SPEC",   cls: "bg-teal-100 text-teal-700",     type: "spec" };

  // PET/CT
  if (/\bct\b|attenuat|transmi/.test(d))         return { label: "CT",     cls: "bg-slate-100 text-slate-700",   type: "ct" };
  if (/\bpt\b|\bpet\b|emission/.test(d))         return { label: "PET",    cls: "bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300",   type: "pet" };

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

  // Clinical intake (patient onboarding) — used to prefer the receptionist-entered
  // referrer over the DICOM ReferringPhysicianName tag, which is frequently an
  // unreliable placeholder rather than curated clinical data.
  const [clinical, setClinical] = useState<ClinicalForStudy | null>(null);
  useEffect(() => {
    let active = true;
    api.onboarding
      .getClinical(uid)
      .then((c) => { if (active) setClinical(c && Object.keys(c).length > 0 ? c : null); })
      .catch(() => { /* no order linked — fall back to DICOM value */ });
    return () => { active = false; };
  }, [uid]);
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


  const [pdfLoading, setPdfLoading] = useState(false);

  // "More" quick-tools overflow menu
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!moreOpen) return;
    const onDown = (e: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) setMoreOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [moreOpen]);

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
        // Default to the first result ONLY when nothing is selected yet. Uses a
        // functional update so the 10 s poll reads the CURRENT selection (not the
        // stale closure value) — otherwise switching modality would snap back.
        if (r.length > 0) setSelectedUsecase((prev) => prev ?? r[0].usecase_name);
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
  // open CPT / Protocol / Prior panel mid-view.
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

  if (loading) return <p className="text-gray-500 dark:text-gray-400 p-4">Loading study...</p>;
  if (!study) return <p className="text-red-500 dark:text-red-400 p-4">Study not found</p>;

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
      <Link href="/worklist" className="inline-flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
        <ArrowLeft className="w-4 h-4" /> Back to Worklist
      </Link>

      {/* Patient hero — identity, demographics, reading status + actions */}
      {(() => {
        const rs = study.reading_status || "unread";
        const btn = "press px-3 py-1.5 text-xs font-medium rounded-lg transition-colors disabled:opacity-50";
        const grad = heroModalityGrad(study.modality);
        const initials =
          formatPatientName(study.patient_name).split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";

        // Demographic line sits right under the name — the scannable pattern used
        // in PACS/EHR patient banners — so the chip strip below doesn't repeat it.
        // Age prefers the DICOM tag (compacted from "022Y" to "22Y"); when the
        // scan carries no age at all, falls back to the onboarding-entered band.
        const ageDisplay = compactAge(study.patient_age) || clinical?.age_band || null;
        const subtitle = [study.patient_sex, ageDisplay, study.modality, formatDate(study.study_date)]
          .filter(Boolean);

        // Icon-labelled chips for the rest of the study metadata (no duplication
        // with the subtitle above); core fields always show for cross-modality
        // consistency, optional ones only when populated. Referring prefers the
        // onboarding-entered referrer over the raw DICOM tag — see the `clinical`
        // fetch above for why.
        const referring = clinical?.referrer || study.referring_physician;
        const chips: { icon: typeof Hash; label: string; value: string }[] = [
          { icon: Hash, label: "MRN", value: study.patient_id || "—" },
          { icon: FileText, label: "Accession", value: study.accession_number || "—" },
          ...(study.body_part_examined ? [{ icon: Stethoscope, label: "Body Part", value: study.body_part_examined }] : []),
          ...(referring ? [{ icon: User, label: "Referring", value: referring }] : []),
          ...(study.institution_name ? [{ icon: Building2, label: "Institution", value: study.institution_name }] : []),
          { icon: Layers, label: "Series", value: String(study.series.length) },
        ];

        return (
          <div className="glass rounded-2xl overflow-hidden">
            {/* Modality-tinted accent bar */}
            <div className={`h-1 bg-gradient-to-r ${grad}`} />

            {/* Identity + actions */}
            <div className="px-5 sm:px-6 pt-5 pb-4 flex items-start justify-between gap-5 flex-wrap">
              <div className="flex items-start gap-4 min-w-0">
                <div className={`relative grid place-items-center w-16 h-16 rounded-2xl bg-gradient-to-br ${grad} text-white text-xl font-bold shrink-0 shadow-glow ring-4 ring-black/[0.03] dark:ring-white/[0.06]`}>
                  {initials}
                </div>
                <div className="min-w-0">
                  <div className="flex items-center gap-3 flex-wrap">
                    <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 truncate">
                      {formatPatientName(study.patient_name)}
                    </h1>
                    <StatusBadge variant="reading" status={rs} assignedTo={study.assigned_to_username} />
                  </div>
                  {subtitle.length > 0 && (
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 flex items-center gap-1.5 flex-wrap">
                      {subtitle.map((part, i) => (
                        <span key={i} className="flex items-center gap-1.5">
                          {i > 0 && <span className="text-gray-300 dark:text-gray-600">&middot;</span>}
                          <span className={i === subtitle.length - 1 ? "" : "font-medium text-gray-700 dark:text-gray-300"}>{part}</span>
                        </span>
                      ))}
                    </p>
                  )}
                  {study.study_description && (
                    <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate">{study.study_description}</p>
                  )}
                </div>
              </div>

              {/* Actions — grouped into one visual cluster instead of floating pieces */}
              <div className="flex flex-col items-end gap-2 shrink-0">
                <a
                  href={viewerUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn-gradient flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-xl"
                >
                  <ExternalLink className="w-4 h-4" />
                  {hasPetSeries ? "Open in OHIF" : "Open Viewer"}
                </a>
                <div className="flex items-center gap-2 flex-wrap justify-end p-1 rounded-xl bg-black/[0.03] dark:bg-white/[0.04] border border-border">
                  {rs === "unread" && (
                    <>
                      <button disabled={readingBusy} onClick={() => runReading(() => api.reading.claim(uid))}
                        className={`${btn} text-white bg-primary-600 hover:bg-primary-700`}>Claim</button>
                      <button disabled={readingBusy} onClick={() => runReading(() => api.reading.autoAssign(uid))}
                        className={`${btn} text-primary-700 dark:text-primary-300 hover:bg-primary-50 dark:hover:bg-primary-950`}>Auto-assign</button>
                    </>
                  )}
                  {rs === "in_progress" && (
                    <>
                      <button disabled={readingBusy} onClick={() => runReading(() => api.reading.report(uid))}
                        className={`${btn} text-white bg-amber-600 hover:bg-amber-700`}>Mark Reported</button>
                      <button disabled={readingBusy} onClick={() => runReading(() => api.reading.unclaim(uid))}
                        className={`${btn} text-gray-600 dark:text-gray-400 hover:bg-black/5 dark:hover:bg-white/5`}>Release</button>
                    </>
                  )}
                  {rs === "reported" && (
                    <button disabled={readingBusy} onClick={() => setPendingSign(true)}
                      className={`${btn} text-white bg-green-600 hover:bg-green-700`}>Sign Off</button>
                  )}
                  {rs === "signed" && (
                    <span className="px-3 py-1.5 text-xs text-green-600 dark:text-green-400 flex items-center gap-1">
                      <CheckCircle className="w-4 h-4" /> Signed
                    </span>
                  )}
                </div>
                {(study.tat_signoff_minutes != null || study.tat_report_minutes != null) && (
                  <span className="inline-flex items-center gap-1 text-xs text-gray-400 dark:text-gray-500">
                    <Clock className="w-3 h-3" />
                    {study.tat_signoff_minutes != null
                      ? `Turnaround: ${study.tat_signoff_minutes} min`
                      : `Report TAT: ${study.tat_report_minutes} min`}
                  </span>
                )}
              </div>
            </div>

            {/* Detail strip — icon-labelled chips, dividers for scannability */}
            <div className="border-t border-border bg-black/[0.02] dark:bg-white/[0.02] px-5 sm:px-6 py-3.5">
              <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
                {chips.map((c, i) => {
                  const Icon = c.icon;
                  return (
                    <div key={c.label} className="flex items-center gap-4">
                      {i > 0 && <span className="hidden sm:block w-px h-8 bg-border" />}
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="grid place-items-center w-8 h-8 rounded-lg bg-accent/10 text-accent shrink-0">
                          <Icon className="w-4 h-4" />
                        </span>
                        <div className="min-w-0">
                          <dt className="text-[10px] font-medium uppercase tracking-wider text-gray-400 dark:text-gray-500">{c.label}</dt>
                          <dd className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate max-w-[220px] sm:max-w-[280px]" title={c.value}>{c.value}</dd>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {readingError && <p className="px-6 pb-3 text-xs text-red-600 dark:text-red-400">{readingError}</p>}
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

      {/* Two-column workspace: viewer/results (left) + metadata sidebar (right) */}
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-5 items-start">
        {/* Metadata sidebar — placed on the right via order, sticky on scroll */}
        <aside className="space-y-4 lg:order-2 lg:sticky lg:top-4">
          <div className="glass rounded-2xl p-4">
            <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">Series</h2>
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
                    className={`text-sm border-b border-gray-50 dark:border-gray-800 pb-2 ${isSetup ? "opacity-50" : ""}`}
                  >
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {badge && (
                        <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide shrink-0 ${badge.cls}`}>
                          {badge.label}
                        </span>
                      )}
                      <span className={`font-medium truncate ${isSetup ? "text-gray-500 dark:text-gray-400" : "text-gray-900 dark:text-gray-100"}`}>
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
          <div className="mt-2 pt-2 border-t border-gray-50 dark:border-gray-800 flex flex-wrap gap-1.5">
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

        <div className="glass rounded-2xl p-4">
          <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">AI Jobs</h2>
          {jobs.length === 0 ? (
            <p className="text-sm text-gray-400 dark:text-gray-500">No jobs run yet</p>
          ) : (
            <div className="space-y-2 max-h-80 overflow-y-auto pr-1 -mr-1">
              {jobs.map((job) => (
                <div key={job.id} className="border border-gray-100 dark:border-gray-800 rounded-lg p-2.5">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                      {job.usecase_name.replace(/_/g, " ")}
                    </span>
                    <StatusBadge variant="job" status={job.status} />
                  </div>
                  {job.status_message && <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{job.status_message}</p>}
                  {job.progress > 0 && job.status !== "completed" && (
                    <div className="mt-2 bg-gray-100 dark:bg-gray-800 rounded-full h-1.5">
                      <div
                        className="bg-primary-600 h-1.5 rounded-full transition-all"
                        style={{ width: `${job.progress * 100}%` }}
                      />
                    </div>
                  )}
                  {job.error_detail && <p className="text-xs text-red-500 dark:text-red-400 mt-1 truncate">{job.error_detail}</p>}
                </div>
              ))}
            </div>
          )}
          </div>
        </aside>

        {/* Main column — viewer + AI results */}
        <div className="min-w-0 lg:order-1 space-y-5">
        {/* Quick tools — dashboard-style action tiles */}
        {results.length > 0 && selectedResult && (() => {
          const uc = selectedResult.usecase_name;
          const hasReport = uc === "mammography" || ["pet_ct", "pet_ct_brain"].includes(uc) || isCtReportUsecase(uc);
          const reportDesc = uc === "mammography" ? "Mammography report"
            : ["pet_ct", "pet_ct_brain"].includes(uc) ? "PET-CT report" : "AI clinical report";
          type Tool = {
            icon: typeof FileText; label: string; desc: string;
            href?: string; onClick?: () => void; disabled?: boolean; accent?: boolean;
          };
          const tools: Tool[] = [
            ...(hasReport ? [{ icon: FileText, label: "Report", desc: reportDesc, href: `/study/${uid}/report/${uc}`, accent: true }] : []),
            { icon: FileDown, label: "Download PDF", desc: "Save report as PDF", onClick: handleDownloadPdf, disabled: pdfLoading },
            { icon: Share2, label: "Share", desc: "Referrer portal link", onClick: handleCreateShareLink, disabled: shareLoading },
          ];
          const cardCls = "group glass rounded-2xl p-3.5 hover-lift text-left disabled:opacity-50 disabled:cursor-not-allowed";
          const menuItem = "w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-gray-700 dark:text-gray-300 hover:bg-accent/10 transition-colors text-left";
          return (
            <div className="space-y-3">
              {results.length > 1 && (
                <div className="flex items-center gap-1 p-1 rounded-xl bg-black/5 dark:bg-white/5 border border-border w-fit">
                  {results.map((r) => {
                    const active = selectedUsecase === r.usecase_name;
                    return (
                      <button key={r.usecase_name} onClick={() => setSelectedUsecase(r.usecase_name)}
                        className={`press px-4 py-1.5 rounded-lg text-sm font-semibold transition-all whitespace-nowrap ${
                          active ? "bg-primary-600 text-white shadow-glow-sm" : "text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white"
                        }`}>
                        {r.usecase_name.replace(/_/g, " ").replace(/\b\w/g, (c: string) => c.toUpperCase())}
                      </button>
                    );
                  })}
                </div>
              )}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {tools.map((t) => {
                  const Icon = t.icon;
                  const inner = (
                    <>
                      <span className={`grid place-items-center w-9 h-9 rounded-lg mb-2 transition-transform duration-300 group-hover:scale-110 ${t.accent ? "bg-accent-gradient text-white shadow-glow-sm" : "bg-accent/10 ring-1 ring-accent/20 text-accent"}`}>
                        <Icon className="w-5 h-5" />
                      </span>
                      <p className="text-sm font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-1">
                        {t.label}
                        <ArrowUpRight className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 transition-all group-hover:translate-x-0.5 group-hover:-translate-y-0.5 group-hover:text-accent" />
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{t.desc}</p>
                    </>
                  );
                  return t.href ? (
                    <Link key={t.label} href={t.href} className={cardCls}>{inner}</Link>
                  ) : (
                    <button key={t.label} type="button" onClick={t.onClick} disabled={t.disabled} className={cardCls}>{inner}</button>
                  );
                })}

                {/* More — tile that opens an overflow menu */}
                <div className="relative" ref={moreRef}>
                  <button type="button" onClick={() => setMoreOpen((o) => !o)} aria-expanded={moreOpen} className={`${cardCls} w-full`}>
                    <span className="grid place-items-center w-9 h-9 rounded-lg mb-2 bg-accent/10 ring-1 ring-accent/20 text-accent transition-transform duration-300 group-hover:scale-110">
                      <MoreHorizontal className="w-5 h-5" />
                    </span>
                    <p className="text-sm font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-1">
                      More
                      <ChevronDown className={`w-3.5 h-3.5 text-gray-400 dark:text-gray-500 transition-transform ${moreOpen ? "rotate-180" : ""}`} />
                    </p>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">More actions</p>
                  </button>
                  {moreOpen && (
                    <div className="absolute right-0 top-full mt-2 w-60 glass-raised rounded-xl shadow-glow-lg p-1.5 z-30 animate-scale-in">
                      <button onClick={() => { setMoreOpen(false); loadCptSuggestions(); }} className={menuItem}>
                        <DollarSign className="w-4 h-4 text-emerald-500 shrink-0" /> CPT Codes
                      </button>
                      <button onClick={() => { setMoreOpen(false); loadProtocolCheck(); }} className={menuItem}>
                        <Stethoscope className="w-4 h-4 text-violet-500 shrink-0" /> Protocol Check
                      </button>
                      <button onClick={() => { setMoreOpen(false); loadPriorComparison(); }} className={menuItem}>
                        <ArrowLeftRight className="w-4 h-4 text-amber-500 shrink-0" /> Prior Comparison
                      </button>
                      {versions.length >= 2 && (
                        <Link href={`/compare?a=${versions[1].id}&b=${versions[0].id}`} onClick={() => setMoreOpen(false)} className={menuItem}>
                          <ArrowLeftRight className="w-4 h-4 text-primary-500 shrink-0" /> Compare Versions
                        </Link>
                      )}
                      <Link href={`/study/${uid}/delivery`} onClick={() => setMoreOpen(false)} className={menuItem}>
                        <Truck className="w-4 h-4 text-gray-500 dark:text-gray-400 shrink-0" /> Delivery Status
                      </Link>
                      {study.patient_id && (
                        <Link href={`/admin/patients/${encodeURIComponent(study.patient_id)}/trend/${selectedUsecase}`} onClick={() => setMoreOpen(false)} className={menuItem}>
                          <TrendingUp className="w-4 h-4 text-purple-500 shrink-0" /> Trend
                        </Link>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })()}
        {/* DICOM Viewer */}
        <div className="glass rounded-2xl accent-top overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">DICOM Viewer</h2>
            {showNativeFused ? (
              <span className="text-xs bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300 px-2 py-0.5 rounded-full font-medium">
                CT + PET
              </span>
            ) : hasPetSeries ? (
              <span className="text-xs bg-orange-100 dark:bg-orange-900 text-orange-700 dark:text-orange-300 px-2 py-0.5 rounded-full font-medium">
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
            className="w-full border-0 min-h-[560px] max-h-[1100px]"
            style={{ height: hasPetSeries ? "calc(100vh - 180px)" : "calc(100vh - 210px)" }}
            title={hasPetSeries ? "OHIF PET/CT Viewer" : "DICOM Viewer"}
            allow="fullscreen"
          />
        )}
      </div>

      {/* AI results — panels + report */}
      {results.length > 0 && (
        <div className="space-y-4">
          {/* Share link display */}
          {shareLink && (
            <div className="mb-4 bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded-lg p-4 flex items-center gap-3">
              <Link2 className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-blue-700 dark:text-blue-300 mb-1">Referring Physician Portal Link (expires in 7 days)</p>
                <p className="text-sm text-blue-800 font-mono truncate">{shareLink}</p>
              </div>
              <button
                onClick={copyShareLink}
                className="px-3 py-1.5 text-xs font-medium text-blue-700 dark:text-blue-300 bg-blue-100 dark:bg-blue-900 hover:bg-blue-200 rounded-lg transition-colors shrink-0"
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
            <div className="mb-4 glass rounded-2xl overflow-hidden">
              <button
                onClick={() => setCptOpen(!cptOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-white/5 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <DollarSign className="w-4 h-4 text-emerald-600 dark:text-emerald-400" />
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">CPT Billing Code Suggestions</span>
                  <span className="text-xs bg-emerald-100 dark:bg-emerald-900 text-emerald-700 dark:text-emerald-300 px-2 py-0.5 rounded-full font-medium">
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
                          cpt.category === "addon" ? "border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-surface-raised" : "border-emerald-100 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950"
                        }`}
                      >
                        <div className="shrink-0">
                          <span className={`text-sm font-bold font-mono ${cpt.category === "addon" ? "text-gray-600 dark:text-gray-400" : "text-emerald-700 dark:text-emerald-300"}`}>
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
                          <span className="text-xs font-medium text-gray-600 dark:text-gray-400">
                            {(cpt.confidence * 100).toFixed(0)}% confidence
                          </span>
                          <div className="w-16 bg-gray-200 dark:bg-gray-700 rounded-full h-1 mt-1">
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
            <div className="mb-4 glass rounded-2xl overflow-hidden">
              <button
                onClick={() => setProtocolOpen(!protocolOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-white/5 transition-colors"
              >
                <div className="flex items-center gap-2">
                  {protocolCheck.status === "ok" || protocolCheck.issues.length === 0 ? (
                    <CheckCircle className="w-4 h-4 text-green-500 dark:text-green-400" />
                  ) : (
                    <AlertTriangle className="w-4 h-4 text-amber-500 dark:text-amber-400" />
                  )}
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">Protocol Check</span>
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      protocolCheck.status === "ok" || protocolCheck.issues.length === 0
                        ? "bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300"
                        : "bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300"
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
                              ? "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950"
                              : issue.severity === "warning"
                              ? "border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950"
                              : "border-blue-100 dark:border-blue-900 bg-blue-50 dark:bg-blue-950"
                          }`}
                        >
                          <div className="flex items-center gap-2 mb-1">
                            <AlertTriangle
                              className={`w-3.5 h-3.5 ${
                                issue.severity === "error" ? "text-red-500 dark:text-red-400" :
                                issue.severity === "warning" ? "text-amber-500 dark:text-amber-400" : "text-blue-400"
                              }`}
                            />
                            <span className="text-xs font-semibold text-gray-700 dark:text-gray-300">{issue.series_description}</span>
                            <span
                              className={`text-xs px-1.5 py-0.5 rounded font-medium uppercase ${
                                issue.severity === "error" ? "bg-red-200 text-red-700 dark:text-red-300" :
                                issue.severity === "warning" ? "bg-amber-200 text-amber-700 dark:text-amber-300" :
                                "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300"
                              }`}
                            >
                              {issue.severity}
                            </span>
                          </div>
                          <p className="text-sm text-gray-800 dark:text-gray-200">{issue.message}</p>
                          {issue.suggestion && (
                            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
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
            <div className="mb-4 glass rounded-2xl overflow-hidden">
              <button
                onClick={() => setPriorOpen(!priorOpen)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 text-left hover:bg-gray-50 dark:hover:bg-white/5 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <ArrowLeftRight className="w-4 h-4 text-amber-600 dark:text-amber-400" />
                  <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">Prior Study Comparison</span>
                  {priorComparison.delta.days_between !== null && (
                    <span className="text-xs bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300 px-2 py-0.5 rounded-full font-medium">
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


          {/* Professional Report — rendered COMPACT (patient info + imaging only)
              when a dedicated report page exists for this use case, so the study
              page doesn't duplicate the full findings/measurements. */}
          {selectedResult && uiSchema ? (() => {
            // Mammography's compact inline report would show only patient info
            // (already shown at the top) + a link (already in the toolbar), with
            // no imaging — so skip it entirely; the full report is one click away.
            if (selectedResult.usecase_name === "mammography") return null;
            const hasDedicatedReport =
              ["pet_ct", "pet_ct_brain"].includes(selectedResult.usecase_name) ||
              isCtReportUsecase(selectedResult.usecase_name);
            return (
              <ReportView
                study={study}
                result={selectedResult}
                uiSchema={uiSchema}
                compact={hasDedicatedReport}
                reportHref={hasDedicatedReport ? `/study/${uid}/report/${selectedResult.usecase_name}` : undefined}
              />
            );
          })() : selectedUsecase ? (
            <div className="glass rounded-2xl p-8 text-center text-gray-400 dark:text-gray-500">
              Loading report...
            </div>
          ) : null}
        </div>
      )}
        </div>
      </div>
    </div>
  );
}
