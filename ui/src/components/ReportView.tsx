"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { Study, Result, getPreviewUrl, getArtifactUrl, getFusedUrl } from "@/lib/api";
import { isCtReportUsecase } from "@/lib/ctReport";
import { QAPanel } from "./QAPanel";
import { FusedViewer } from "./FusedViewer";
import {
  formatValue,
  formatDate,
  formatDateTime,
  formatPatientName,
  getNestedValue,
} from "@/lib/format";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { AIProvenanceBanner } from "@/components/ui/AIProvenanceBanner";
import { AuthImg } from "@/components/ui/AuthImg";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import {
  Printer,
  AlertTriangle,
  CheckCircle2,
  Download,
  FileText,
  Maximize2,
  X,
} from "lucide-react";

interface ReportViewProps {
  study: Study;
  result: Result;
  uiSchema: any;
  /** Study-page inline mode: keep patient info + imaging (flagged/sampled slices,
      fused/overlay images) but hide the findings/measurements/QA sections that are
      shown in full on the dedicated report page — avoids duplicating the report. */
  compact?: boolean;
  /** Link to the full dedicated report (shown as a CTA in compact mode). */
  reportHref?: string;
}

const VIEWS = ["axial", "coronal", "sagittal"] as const;
const PET_USECASES = ["pet_ct", "pet_ct_brain"];

export function ReportView({ study, result, uiSchema, compact = false, reportHref }: ReportViewProps) {
  const summarySection = uiSchema?.sections?.find((s: any) => s.id === "summary");
  const tumorDetected = result.summary?.tumor_detected;
  const [zoomedView, setZoomedView] = useState<string | null>(null);
  const zoomDialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(zoomDialogRef, zoomedView !== null, () => setZoomedView(null));

  // Only activate PET/CT mode when the DICOM study actually contains PT series.
  // A pet_ct pipeline can be run on non-PET data (routing error); in that case
  // show the MRI viewer sections and a pipeline-mismatch warning instead.
  const actuallyHasPet = study.series.some((s) => s.modality === "PT");
  const isPetCt = PET_USECASES.includes(result.usecase_name) && actuallyHasPet;
  const isPipelineMismatch = PET_USECASES.includes(result.usecase_name) && !actuallyHasPet;
  const isAbdomenCt = isCtReportUsecase(result.usecase_name); // CT-report family (abdomen_ct + ct_*)
  const isAbdomenScan = isAbdomenCt; // full-volume MedGemma scan (flagged slices)
  const isAbdomenCtLike = isAbdomenCt;
  // Mammography is 2D — the axial/coronal/sagittal segmentation-overlay previews
  // don't apply and would always render "Preview not available", so skip them.
  const isMammography = result.usecase_name === "mammography";

  return (
    <div className="report-container">
      {/* Header — redundant with the study-page patient card, so hidden in compact */}
      {!compact && (
      <div className="report-header">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-bold text-primary-900 dark:text-primary-200">
              {uiSchema?.title || "AI Analysis Report"}
            </h1>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
              {uiSchema?.description || ""}
            </p>
            <div className="mt-2">
              <StatusBadge
                variant="reading"
                status={study.reading_status || "unread"}
                assignedTo={study.assigned_to_username}
              />
            </div>
          </div>
          <button
            onClick={() => window.print()}
            className="no-print flex items-center gap-2 px-3 py-2 text-sm bg-primary-900 text-white rounded-lg hover:bg-primary-800 transition-colors"
          >
            <Printer className="w-4 h-4" />
            Print
          </button>
        </div>

        {/* Patient & Study Info Grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-2 mt-5 text-sm">
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Patient</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">
              {formatPatientName(study.patient_name)}
            </p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">MRN</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">{study.patient_id || "-"}</p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Study Date</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">{formatDate(study.study_date)}</p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Accession</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">
              {study.accession_number || "-"}
            </p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Modality</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">{study.modality || "-"}</p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Body Part</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">
              {study.body_part_examined || "-"}
            </p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Referring</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">
              {study.referring_physician || "-"}
            </p>
          </div>
          <div>
            <span className="text-gray-400 dark:text-gray-500 text-xs uppercase tracking-wider">Institution</span>
            <p className="font-semibold text-gray-900 dark:text-gray-100">
              {study.institution_name || "-"}
            </p>
          </div>
        </div>
      </div>
      )}

      {/* Compact mode: point to the full report (findings live there). */}
      {compact && reportHref && (
        <div className="report-section flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Full findings, measurements and QA are in the detailed report.
          </p>
          <Link
            href={reportHref}
            className="no-print inline-flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold rounded-lg bg-primary-900 text-white hover:bg-primary-800 transition-colors"
          >
            <FileText className="w-4 h-4" /> Open full report
          </Link>
        </div>
      )}

      {/* Pipeline mismatch warning */}
      {isPipelineMismatch && (
        <div className="report-section flex items-start gap-3 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg px-4 py-3">
          <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800 dark:text-amber-300">Pipeline mismatch — results are unreliable</p>
            <p className="text-xs text-amber-700 dark:text-amber-300 mt-1 leading-relaxed">
              The <strong>{result.usecase_name.replace(/_/g, " ")}</strong> pipeline was run on
              this study, but no PET series (modality PT) were found in the DICOM data.
              SUV measurements were derived from MRI signal intensity and have no clinical meaning.
              Delete this result and re-run with the correct pipeline (e.g. <strong>brain_mri</strong>).
            </p>
          </div>
        </div>
      )}

      {/* Final Diagnosis Banner */}
      {!compact && (() => {
        const diagnosis: string | undefined =
          result.summary?.diagnosis as string | undefined;
        if (!diagnosis) return null;
        const isPositive = diagnosis.toLowerCase().startsWith("tumor positive");
        return (
          <div
            className="report-section"
            style={{
              borderLeft: `4px solid ${isPositive ? "#dc2626" : "#16a34a"}`,
              paddingLeft: "1.25rem",
            }}
          >
            <h2 className="report-section-title flex items-center gap-2">
              {isPositive ? (
                <AlertTriangle className="w-5 h-5 text-red-600 dark:text-red-400" />
              ) : (
                <CheckCircle2 className="w-5 h-5 text-green-600 dark:text-green-400" />
              )}
              Final Diagnosis
            </h2>
            <div
              className={`mt-2 p-4 rounded-lg text-sm font-semibold leading-relaxed ${
                isPositive
                  ? "bg-red-50 dark:bg-red-950 text-red-900 dark:text-red-200 border border-red-200 dark:border-red-800"
                  : "bg-green-50 dark:bg-green-950 text-green-900 dark:text-green-200 border border-green-200 dark:border-green-800"
              }`}
            >
              {diagnosis}
            </div>
          </div>
        );
      })()}

      {/* ── Abdomen CT: MedGemma free-text report + sampled slices ── */}
      {isAbdomenCtLike && (
        <>
          {!compact && (() => {
            const report: any = result.summary?.ai_report;
            if (!report || (!report.findings && !report.impression)) {
              return (
                <div className="report-section flex items-start gap-3 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg px-4 py-3">
                  <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
                  <div>
                    <p className="text-sm font-semibold text-amber-800 dark:text-amber-300">No AI report generated</p>
                    <p className="text-xs text-amber-700 dark:text-amber-300 mt-1 leading-relaxed">
                      The raw slices below were rendered, but the local MedGemma model did not
                      produce a report (it may be disabled or unreachable). Review the slices
                      directly.
                    </p>
                  </div>
                </div>
              );
            }
            return (
              <div className="report-section" style={{ borderLeft: "4px solid #2563eb", paddingLeft: "1.25rem" }}>
                <h2 className="report-section-title flex items-center gap-2">
                  <FileText className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                  AI Radiological Report
                </h2>
                {report.findings && (
                  <div className="mt-2">
                    <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
                      Findings
                    </h3>
                    <p className="text-sm text-gray-800 dark:text-gray-200 leading-relaxed whitespace-pre-line">
                      {report.findings}
                    </p>
                  </div>
                )}
                {report.impression && (
                  <div className="mt-4">
                    <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
                      Impression
                    </h3>
                    <p className="text-sm text-gray-900 dark:text-gray-100 font-medium leading-relaxed whitespace-pre-line">
                      {report.impression}
                    </p>
                  </div>
                )}
                {(report.disclaimer || result.summary?.ai_report_provider) && (
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-3 italic">
                    {report.disclaimer}
                    {result.summary?.ai_report_provider
                      ? ` (${result.summary.ai_report_provider})`
                      : ""}
                  </p>
                )}
              </div>
            );
          })()}

          {(() => {
            const slices = result.artifacts.filter(
              (a) => a.artifact_type === `${result.usecase_name}_slice_png`
            );
            if (slices.length === 0) return null;
            return (
              <div className="report-section">
                <div className="flex items-center gap-2 mb-1.5">
                  <h2 className="report-section-title !mb-0">
                    {isAbdomenScan ? "Flagged Slices" : "Sampled Slices"}
                  </h2>
                  <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-primary-50 dark:bg-primary-950 text-primary-700 dark:text-primary-300">
                    {slices.length}
                  </span>
                </div>
                <p className="text-xs text-gray-400 dark:text-gray-500 mb-3.5">
                  {isAbdomenScan
                    ? "MedGemma scanned every slice of the volume and flagged these axial levels as potentially abnormal (superior→inferior) — the report was written from these findings."
                    : "Axial levels sampled evenly across the volume (superior→inferior), each in multiple HU windows (soft-tissue / liver / bone) — the exact images the AI report was written from."}
                </p>
                <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
                  {slices.map((artifact) => (
                    <div
                      key={artifact.name}
                      className="group relative aspect-square bg-black rounded-xl overflow-hidden ring-1 ring-gray-200 dark:ring-white/10 hover:ring-primary-400 dark:hover:ring-primary-500 transition-all"
                    >
                      <AuthImg
                        src={getArtifactUrl(study.study_instance_uid, result.usecase_name, artifact.name)}
                        alt={artifact.name}
                        className="w-full h-full object-contain transition-transform duration-300 group-hover:scale-105"
                        loadingClassName="w-full h-full bg-gray-900 animate-pulse motion-reduce:animate-none"
                        errorClassName="w-full h-full flex items-center justify-center text-gray-500 dark:text-gray-400 text-xs bg-black"
                        fallback="Slice not available"
                      />
                      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/40 to-transparent px-2.5 pt-6 pb-1.5">
                        <span className="block text-[11px] font-medium text-white/90 capitalize truncate">
                          {artifact.name.replace(/\.[^.]+$/, "").replace(/_/g, " ")}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}
        </>
      )}

      {/* Clinical Findings */}
      {!compact && summarySection && (
        <div className="report-section">
          <h2 className="report-section-title flex items-center gap-2">
            <FileText className="w-4 h-4" />
            Clinical Findings
          </h2>
          {tumorDetected !== undefined && (
            <div
              className={`inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold mb-4 ${
                tumorDetected
                  ? "bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300 border border-red-300"
                  : "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300 border border-green-200 dark:border-green-800"
              }`}
            >
              {tumorDetected ? (
                <AlertTriangle className="w-5 h-5" />
              ) : (
                <CheckCircle2 className="w-5 h-5" />
              )}
              {tumorDetected
                ? "POSITIVE — Tumor Detected"
                : "NEGATIVE — No Tumor Detected"}
            </div>
          )}
          <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-3 text-sm">
            {summarySection.fields
              ?.filter((f: any) => f.key !== "tumor_detected" && f.key !== "diagnosis")
              .map((field: any) => {
                const value = field.data_path
                  ? getNestedValue(result, field.data_path)
                  : getNestedValue(result, `summary.${field.key}`);
                const isVolumeField =
                  field.key === "total_lesion_volume_ml" &&
                  tumorDetected &&
                  typeof value === "number" &&
                  value > 0;
                return (
                  <div key={field.key}>
                    <dt className="text-gray-500 dark:text-gray-400 text-xs uppercase tracking-wider">
                      {field.label}
                    </dt>
                    <dd
                      className={`font-medium mt-0.5 ${
                        isVolumeField
                          ? "text-red-700 dark:text-red-300 text-lg font-bold"
                          : "text-gray-900 dark:text-gray-100"
                      }`}
                    >
                      {formatValue(value, field.format, field.precision, field.unit)}
                    </dd>
                  </div>
                );
              })}
          </dl>
        </div>
      )}

      {/* AI-Detected Abnormal Findings (pathology: abdomen organs, chest lungs,
          coronary stenosis, dedicated lesion model). Data-driven from the result
          so it appears for any use case that emits these keys. */}
      {!compact && (() => {
        const findings: any[] = Array.isArray(result.summary?.abnormal_findings)
          ? result.summary.abnormal_findings
          : [];
        const segments: any[] = Array.isArray(result.measurements?.segments)
          ? result.measurements.segments
          : [];
        const lesionDetected = result.summary?.lesion_detected === true;
        const lesionCount = result.summary?.lesion_count ?? 0;
        if (findings.length === 0 && segments.length === 0 && !lesionDetected) return null;

        const sevCls = (sev?: string): string =>
          (({
            marked: "bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300 border-red-300",
            severe: "bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300 border-red-300",
            occluded: "bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300 border-red-300",
            moderate: "bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-300 border-amber-300",
            mild: "bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300 border-yellow-300",
            minimal: "bg-yellow-50 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-300 border-yellow-200 dark:border-yellow-800",
          } as Record<string, string>)[sev || ""] ||
            "bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 border-gray-300 dark:border-gray-600");

        return (
          <div
            className="report-section"
            style={{ borderLeft: "4px solid #d97706", paddingLeft: "1.25rem" }}
          >
            <h2 className="report-section-title flex items-center gap-2">
              <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400" />
              AI-Detected Abnormal Findings
            </h2>

            {lesionDetected && (
              <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-semibold mt-2 bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300 border border-red-300">
                <AlertTriangle className="w-4 h-4" />
                {lesionCount > 0 ? `${lesionCount} lesion(s) detected` : "Lesion detected"}
              </div>
            )}

            {findings.length > 0 && (
              <ul className="mt-3 space-y-2">
                {findings.map((f: any, i: number) => {
                  const title = f.organ || f.finding || f.side || "Finding";
                  const sev = f.severity || f.status;
                  return (
                    <li key={i} className="flex items-start gap-3 text-sm">
                      <span
                        className={`shrink-0 mt-0.5 px-2 py-0.5 rounded-full text-xs font-semibold border ${sevCls(
                          sev
                        )}`}
                      >
                        {f.status && f.status !== "normal" ? `${f.status} · ` : ""}
                        {sev || "finding"}
                      </span>
                      <div>
                        <span className="font-semibold text-gray-900 dark:text-gray-100 capitalize">
                          {String(title).replace(/_/g, " ")}
                        </span>
                        {f.note && (
                          <p className="text-gray-600 dark:text-gray-400 mt-0.5 leading-relaxed">{f.note}</p>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}

            {segments.length > 0 && (
              <div className="mt-4">
                <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                  Per-Vessel Stenosis
                  {result.summary?.cad_rads != null && (
                    <span className="ml-2 normal-case text-gray-700 dark:text-gray-300">
                      · CAD-RADS {result.summary.cad_rads}
                    </span>
                  )}
                </h3>
                <table className="w-full measurement-table">
                  <thead>
                    <tr>
                      <th>Vessel</th>
                      <th>Stenosis</th>
                      <th>Grade</th>
                      <th>Min lumen (mm)</th>
                      <th>Reference (mm)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {segments.map((s: any, i: number) => (
                      <tr key={i} className={s.stenosis_pct >= 50 ? "bg-red-50 dark:bg-red-950" : ""}>
                        <td className="font-semibold text-gray-900 dark:text-gray-100">
                          {s.name || s.vessel || `Vessel ${i + 1}`}
                        </td>
                        <td className={s.stenosis_pct >= 50 ? "text-red-700 dark:text-red-300 font-bold" : ""}>
                          {typeof s.stenosis_pct === "number"
                            ? `${s.stenosis_pct.toFixed(0)}%`
                            : "-"}
                        </td>
                        <td>
                          <span
                            className={`px-2 py-0.5 rounded-full text-xs font-semibold border ${sevCls(
                              s.grade
                            )}`}
                          >
                            {s.grade || "-"}
                          </span>
                        </td>
                        <td>{s.min_lumen_diameter_mm ?? "-"}</td>
                        <td>{s.reference_diameter_mm ?? "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <p className="text-xs text-gray-400 dark:text-gray-500 mt-3 italic">
              AI screening output — not a diagnosis. Review against the images and clinical
              context.
            </p>
          </div>
        );
      })()}

      {/* Anatomical Region Summary (PET-CT: group lesions by region) */}
      {!compact && (() => {
        const lesions: any[] = result.measurements?.lesions;
        if (!Array.isArray(lesions) || lesions.length === 0) return null;
        if (!lesions[0]?.anatomical_region) return null;
        const regionMap: Record<string, { count: number; suvMax: number; mtv: number }> = {};
        for (const l of lesions) {
          const region = l.anatomical_region || "Unknown";
          if (!regionMap[region]) regionMap[region] = { count: 0, suvMax: 0, mtv: 0 };
          regionMap[region].count += 1;
          regionMap[region].suvMax = Math.max(regionMap[region].suvMax, l.suv_max ?? 0);
          regionMap[region].mtv += l.volume_ml ?? 0;
        }
        const regions = Object.entries(regionMap).sort((a, b) => b[1].suvMax - a[1].suvMax);
        return (
          <div className="report-section">
            <h2 className="report-section-title">Disease Distribution by Anatomical Region</h2>
            <table className="w-full measurement-table">
              <thead>
                <tr>
                  <th>Region</th>
                  <th>Lesion Count</th>
                  <th>SUVmax</th>
                  <th>MTV (mL)</th>
                </tr>
              </thead>
              <tbody>
                {regions.map(([region, stats]) => (
                  <tr key={region}>
                    <td className="font-semibold text-gray-900 dark:text-gray-100">{region}</td>
                    <td>{stats.count}</td>
                    <td className={stats.suvMax > 2.5 ? "text-red-700 dark:text-red-300 font-bold" : ""}>{stats.suvMax.toFixed(2)}</td>
                    <td>{stats.mtv.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })()}

      {/* Measurements Table */}
      {!compact && uiSchema?.sections
        ?.filter((s: any) => s.type === "table")
        .map((section: any) => {
          const data = getNestedValue(result, section.data_path);
          if (!data || typeof data !== "object") return null;

          // Normalise: array of row objects  OR  flat key→value dict
          const rows: any[] = Array.isArray(data)
            ? data
            : Object.entries(data).map(([k, v]) => ({ _key: k, _value: v }));

          // Keep entries for legacy bar-chart scaling (flat-dict case only)
          const entries: [string, any][] = Array.isArray(data)
            ? []
            : Object.entries(data);

          const supplementaryData = section.supplementary
            ? getNestedValue(result, section.supplementary.data_path)
            : null;

          // Find the max volume for bar scaling (flat-dict tables only)
          const numericValues = entries.map(([, v]) =>
            typeof v === "number" ? v : 0
          );
          const maxVal = Math.max(...numericValues, 0.01);

          // Map structure names to colors from overlay colormap
          const overlaySection = uiSchema?.sections?.find(
            (s: any) => s.type === "overlay"
          );
          const colormap = overlaySection?.colormap || {};
          const colorEntries = Object.values(colormap) as {
            label: string;
            color: string;
          }[];

          const getRowColor = (key: string): string => {
            const label = key
              .replace(/_/g, " ")
              .toLowerCase();
            const match = colorEntries.find(
              (c) => c.label.toLowerCase() === label
            );
            return match?.color || "#dc2626";
          };

          return (
            <div key={section.id} className="report-section">
              <h2 className="report-section-title">{section.title}</h2>
              <table className="w-full measurement-table">
                <thead>
                  <tr>
                    {section.columns?.map((col: any) => (
                      <th key={col.key}>{col.label}</th>
                    ))}
                    {section.supplementary && (
                      <th>{section.supplementary.label}</th>
                    )}
                    {tumorDetected && <th>Distribution</th>}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row: any, rowIdx: number) => {
                    // For flat-dict rows: key = row._key, value = row._value
                    // For array rows: key = row.id or index, value not used directly
                    const key: string = row._key ?? String(row.id ?? rowIdx);
                    const value: any = row._value;
                    const numVal = typeof value === "number" ? value : 0;
                    const hasVolume = numVal > 0 && tumorDetected;
                    const barColor = getRowColor(key);
                    const barWidth = Math.max(
                      (numVal / maxVal) * 100,
                      hasVolume ? 3 : 0
                    );

                    return (
                      <tr
                        key={key}
                        className={hasVolume ? "bg-red-50 dark:bg-red-950" : ""}
                        style={
                          hasVolume
                            ? { borderLeft: `3px solid ${barColor}` }
                            : undefined
                        }
                      >
                        {section.columns?.map((col: any) => (
                          <td
                            key={col.key}
                            className={
                              col.key === "_key"
                                ? `font-semibold ${
                                    hasVolume ? "text-red-900 dark:text-red-200" : "text-gray-900 dark:text-gray-100"
                                  }`
                                : col.key === "_value" && hasVolume
                                ? "text-red-700 dark:text-red-300 font-bold"
                                : ""
                            }
                          >
                            {col.key === "_key"
                              ? key
                                  .replace(/_/g, " ")
                                  .replace(/\b\w/g, (c: string) =>
                                    c.toUpperCase()
                                  )
                              : formatValue(
                                  row[col.key] !== undefined ? row[col.key] : value,
                                  col.format,
                                  col.precision
                                )}
                          </td>
                        ))}
                        {supplementaryData && (
                          <td
                            className={
                              hasVolume ? "text-red-600 dark:text-red-400 font-semibold" : ""
                            }
                          >
                            {formatValue(
                              supplementaryData[key],
                              section.supplementary.format,
                              section.supplementary.precision
                            )}
                          </td>
                        )}
                        {tumorDetected && (
                          <td className="w-32">
                            {hasVolume && (
                              <div className="flex items-center gap-2">
                                <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                                  <div
                                    className="h-full rounded-full transition-all"
                                    style={{
                                      width: `${barWidth}%`,
                                      backgroundColor: barColor,
                                    }}
                                  />
                                </div>
                              </div>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {/* Supplementary measurements info */}
              {result.measurements?.voxel_spacing && (
                <div className="mt-3 flex gap-6 text-xs text-gray-500 dark:text-gray-400">
                  <span>
                    Voxel Spacing:{" "}
                    {Array.isArray(result.measurements.voxel_spacing)
                      ? result.measurements.voxel_spacing
                          .map((v: number) => v.toFixed(1))
                          .join(" x ")
                      : result.measurements.voxel_spacing}{" "}
                    mm
                  </span>
                  {result.measurements.image_dimensions && (
                    <span>
                      Image:{" "}
                      {Array.isArray(result.measurements.image_dimensions)
                        ? result.measurements.image_dimensions.join(" x ")
                        : result.measurements.image_dimensions}
                    </span>
                  )}
                </div>
              )}
            </div>
          );
        })}

      {/* ── PET/CT: Fused images via on-demand endpoint ── */}
      {isPetCt ? (
        <>
          {/* In compact (study-page) mode the main viewer already shows the
              interactive fused viewer, so this duplicate is hidden to avoid a
              long, redundant second copy. */}
          {!compact && (
            <div className="report-section">
              <h2 className="report-section-title">Fused PET/CT Images</h2>
              <p className="text-xs text-gray-400 dark:text-gray-500 mb-3">
                CT anatomy with PET SUV hot-colormap overlay. Only voxels above 20% of the display
                SUVmax are coloured to preserve CT anatomy in low-uptake regions. Scroll through every
                slice in each plane.
              </p>
              <div className="rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
                <FusedViewer
                  studyUid={study.study_instance_uid}
                  usecase={result.usecase_name}
                  modes={["fused"]}
                />
              </div>
            </div>
          )}

          {/* MIP images — shown when present in artifacts */}
          {(() => {
            const mipSection = uiSchema?.sections?.find(
              (s: any) => s.type === "image" && s.artifact_filter === "mip_png"
            );
            const mipArtifacts = result.artifacts.filter((a) => a.artifact_type === "mip_png");
            if (!mipSection || mipArtifacts.length === 0) return null;
            return (
              <div className="report-section">
                <h2 className="report-section-title">{mipSection.title || "Maximum Intensity Projection"}</h2>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {mipArtifacts.map((artifact) => {
                    const label = artifact.name.replace(/\.[^.]+$/, "").split("_").slice(1).join(" ") || artifact.name;
                    return (
                      <div key={artifact.name}>
                        <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1.5 text-center">
                          {label}
                        </div>
                        <div className="h-[360px] bg-black rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
                          <AuthImg
                            src={getArtifactUrl(study.study_instance_uid, result.usecase_name, artifact.name)}
                            alt={artifact.name}
                            className="w-full h-full object-contain"
                            loadingClassName="w-full h-full bg-gray-900 animate-pulse motion-reduce:animate-none"
                            errorClassName="w-full h-full flex items-center justify-center text-gray-500 dark:text-gray-400 text-xs bg-black"
                            fallback="MIP not available"
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </>
      ) : isAbdomenCtLike || isMammography ? (
        /* abdomen_ct: slices render in the block above; mammography is 2D — no
           3-plane overlay in either case. */
        null
      ) : (
        /* ── MRI / non-PET: Segmentation overlay via preview endpoint ── */
        <div className="report-section">
          <h2 className="report-section-title">Imaging — Segmentation Overlay</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {VIEWS.map((view) => {
              const url = getPreviewUrl(study.study_instance_uid, result.usecase_name, view);
              return (
                <div key={view} className="relative group">
                  <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1.5 text-center">
                    {view}
                  </div>
                  <div
                    className="relative aspect-[4/3] bg-black rounded-lg overflow-hidden cursor-pointer border border-gray-200 dark:border-gray-700 hover:border-primary-400 transition-colors"
                    onClick={() => setZoomedView(view)}
                  >
                    <AuthImg
                      src={url}
                      alt={`${view} segmentation overlay`}
                      className="w-full h-full object-contain"
                      loadingClassName="w-full h-full bg-gray-900 animate-pulse motion-reduce:animate-none"
                      errorClassName="w-full h-full flex items-center justify-center text-gray-500 dark:text-gray-400 text-xs bg-black"
                      fallback="Preview not available"
                    />
                    <div className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity">
                      <Maximize2 className="w-4 h-4 text-white drop-shadow-lg" />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-3 text-center">
            Colored regions indicate AI-detected segmentation overlaid on the scan. Click to enlarge.
          </p>
        </div>
      )}

      {/* Zoomed image modal */}
      {zoomedView && (
        <div
          className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center no-print"
          onClick={() => setZoomedView(null)}
        >
          <div
            ref={zoomDialogRef}
            role="dialog"
            aria-modal="true"
            aria-label={`${zoomedView} view, enlarged`}
            tabIndex={-1}
            className="relative max-w-4xl max-h-[90vh] p-2 outline-none"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => setZoomedView(null)}
              aria-label="Close enlarged view"
              className="absolute -top-3 -right-3 bg-white dark:bg-surface rounded-full p-1.5 shadow-lg z-10 hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              <X className="w-5 h-5 text-gray-700 dark:text-gray-300" />
            </button>
            <div className="text-center text-white text-sm font-medium mb-2 uppercase tracking-wider">
              {zoomedView} View
            </div>
            <AuthImg
              src={
                isPetCt
                  ? getFusedUrl(
                      study.study_instance_uid,
                      result.usecase_name,
                      zoomedView as "axial" | "coronal" | "sagittal"
                    )
                  : getPreviewUrl(
                      study.study_instance_uid,
                      result.usecase_name,
                      zoomedView as "axial" | "coronal" | "sagittal"
                    )
              }
              alt={`${zoomedView} enlarged`}
              className="max-h-[80vh] rounded-lg shadow-2xl"
            />
          </div>
        </div>
      )}

      {/* Quality Assurance */}
      {!compact && (
        <div className="report-section">
          <h2 className="report-section-title">Quality Assurance</h2>
          <QAPanel flags={result.qa_flags} details={result.qa_details} />
        </div>
      )}

      {/* Segmentation Overlay */}
      {!compact && uiSchema?.sections
        ?.filter((s: any) => s.type === "overlay")
        .map((section: any) => {
          const segArtifacts = result.artifacts.filter(
            (a) => a.artifact_type === section.artifact_filter
          );
          if (segArtifacts.length === 0 && !section.colormap) return null;

          return (
            <div key={section.id} className="report-section">
              <h2 className="report-section-title">{section.title}</h2>
              {section.colormap && (
                <div className="flex flex-wrap gap-3 mb-3">
                  {Object.entries(section.colormap).map(
                    ([id, meta]: [string, any]) => (
                      <div
                        key={id}
                        className="flex items-center gap-2 px-3 py-1.5 rounded-lg border"
                        style={{
                          borderColor: meta.color,
                          backgroundColor: `${meta.color}15`,
                        }}
                      >
                        <div
                          className="w-4 h-4 rounded-sm"
                          style={{ backgroundColor: meta.color }}
                        />
                        <span
                          className="text-sm font-medium"
                          style={{ color: meta.color }}
                        >
                          {meta.label}
                        </span>
                      </div>
                    )
                  )}
                </div>
              )}
              {segArtifacts.length > 0 && (
                <div className="space-y-1.5">
                  {segArtifacts.map((a) => (
                    <div
                      key={a.name}
                      className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400"
                    >
                      <Download className="w-3.5 h-3.5" />
                      <span>{a.name}</span>
                      <span className="text-xs text-gray-400 dark:text-gray-500">
                        ({(a.size_bytes / 1024).toFixed(0)} KB)
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}

      {/* Footer */}
      {!compact && (
      <div className="report-footer">
        <div className="flex flex-wrap gap-x-8 gap-y-1 mb-3">
          <span>
            <strong>Model:</strong> {result.model_version}
          </span>
          <span>
            <strong>Checksum:</strong>{" "}
            <code className="text-xs">{result.model_checksum}</code>
          </span>
          <span>
            <strong>Generated:</strong> {formatDateTime(result.created_at)}
          </span>
        </div>
        <AIProvenanceBanner variant="footer" />
      </div>
      )}
    </div>
  );
}
