"use client";

import { useState, useEffect } from "react";
import { Study, Result, getPreviewUrl, getArtifactUrl, getFusedUrl } from "@/lib/api";
import { QAPanel } from "./QAPanel";
import { FusedViewer } from "./FusedViewer";
import {
  formatValue,
  formatDate,
  formatDateTime,
  formatPatientName,
  getNestedValue,
} from "@/lib/format";
import {
  Printer,
  AlertTriangle,
  CheckCircle2,
  Download,
  FileText,
  Maximize2,
  X,
} from "lucide-react";

// Fetches image with Authorization header so JWT-protected /api/artifacts and
// /api/preview endpoints return 200 instead of 401 (browser <img> never sends
// auth headers on its own).
function AuthImage({
  src,
  alt,
  className,
  fallback = "Image not available",
}: {
  src: string;
  alt: string;
  className?: string;
  fallback?: string;
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let revoke: string | null = null;
    const token = typeof window !== "undefined" ? localStorage.getItem("auth_token") : null;
    const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};

    fetch(src, { headers })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        revoke = url;
        setObjectUrl(url);
      })
      .catch(() => setFailed(true));

    return () => {
      if (revoke) URL.revokeObjectURL(revoke);
    };
  }, [src]);

  if (failed)
    return (
      <div className="flex items-center justify-center h-48 text-gray-500 text-xs bg-black rounded-lg">
        {fallback}
      </div>
    );
  if (!objectUrl)
    return <div className="h-48 bg-gray-900 rounded-lg animate-pulse" />;
  /* eslint-disable-next-line @next/next/no-img-element */
  return <img src={objectUrl} alt={alt} className={className} />;
}

interface ReportViewProps {
  study: Study;
  result: Result;
  uiSchema: any;
}

const VIEWS = ["axial", "coronal", "sagittal"] as const;
const PET_USECASES = ["pet_ct", "pet_ct_brain"];

export function ReportView({ study, result, uiSchema }: ReportViewProps) {
  const summarySection = uiSchema?.sections?.find((s: any) => s.id === "summary");
  const tumorDetected = result.summary?.tumor_detected;
  const [zoomedView, setZoomedView] = useState<string | null>(null);

  // Only activate PET/CT mode when the DICOM study actually contains PT series.
  // A pet_ct pipeline can be run on non-PET data (routing error); in that case
  // show the MRI viewer sections and a pipeline-mismatch warning instead.
  const actuallyHasPet = study.series.some((s) => s.modality === "PT");
  const isPetCt = PET_USECASES.includes(result.usecase_name) && actuallyHasPet;
  const isPipelineMismatch = PET_USECASES.includes(result.usecase_name) && !actuallyHasPet;

  return (
    <div className="report-container">
      {/* Header */}
      <div className="report-header">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-bold text-primary-900">
              {uiSchema?.title || "AI Analysis Report"}
            </h1>
            <p className="text-sm text-gray-500 mt-0.5">
              {uiSchema?.description || ""}
            </p>
            {(() => {
              const rs = study.reading_status || "unread";
              const by = study.assigned_to_username;
              const m: Record<string, [string, string]> = {
                unread: ["Unclaimed", "bg-gray-100 text-gray-600"],
                in_progress: [by ? `Reading — ${by}` : "Reading", "bg-blue-100 text-blue-700"],
                reported: [by ? `Reported — ${by}` : "Reported", "bg-amber-100 text-amber-800"],
                signed: [`Signed off${by ? ` — ${by}` : ""}`, "bg-green-100 text-green-700"],
              };
              const [label, cls] = m[rs] || [rs, "bg-gray-100 text-gray-600"];
              return (
                <span className={`inline-block mt-2 px-2.5 py-0.5 rounded-full text-xs font-semibold ${cls}`}>
                  {rs !== "signed" ? "Preliminary · " : ""}{label}
                </span>
              );
            })()}
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
            <span className="text-gray-400 text-xs uppercase tracking-wider">Patient</span>
            <p className="font-semibold text-gray-900">
              {formatPatientName(study.patient_name)}
            </p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">MRN</span>
            <p className="font-semibold text-gray-900">{study.patient_id || "-"}</p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">Study Date</span>
            <p className="font-semibold text-gray-900">{formatDate(study.study_date)}</p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">Accession</span>
            <p className="font-semibold text-gray-900">
              {study.accession_number || "-"}
            </p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">Modality</span>
            <p className="font-semibold text-gray-900">{study.modality || "-"}</p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">Body Part</span>
            <p className="font-semibold text-gray-900">
              {study.body_part_examined || "-"}
            </p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">Referring</span>
            <p className="font-semibold text-gray-900">
              {study.referring_physician || "-"}
            </p>
          </div>
          <div>
            <span className="text-gray-400 text-xs uppercase tracking-wider">Institution</span>
            <p className="font-semibold text-gray-900">
              {study.institution_name || "-"}
            </p>
          </div>
        </div>
      </div>

      {/* Pipeline mismatch warning */}
      {isPipelineMismatch && (
        <div className="report-section flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3">
          <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800">Pipeline mismatch — results are unreliable</p>
            <p className="text-xs text-amber-700 mt-1 leading-relaxed">
              The <strong>{result.usecase_name.replace(/_/g, " ")}</strong> pipeline was run on
              this study, but no PET series (modality PT) were found in the DICOM data.
              SUV measurements were derived from MRI signal intensity and have no clinical meaning.
              Delete this result and re-run with the correct pipeline (e.g. <strong>brain_mri</strong>).
            </p>
          </div>
        </div>
      )}

      {/* Final Diagnosis Banner */}
      {(() => {
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
                <AlertTriangle className="w-5 h-5 text-red-600" />
              ) : (
                <CheckCircle2 className="w-5 h-5 text-green-600" />
              )}
              Final Diagnosis
            </h2>
            <div
              className={`mt-2 p-4 rounded-lg text-sm font-semibold leading-relaxed ${
                isPositive
                  ? "bg-red-50 text-red-900 border border-red-200"
                  : "bg-green-50 text-green-900 border border-green-200"
              }`}
            >
              {diagnosis}
            </div>
          </div>
        );
      })()}

      {/* Clinical Findings */}
      {summarySection && (
        <div className="report-section">
          <h2 className="report-section-title flex items-center gap-2">
            <FileText className="w-4 h-4" />
            Clinical Findings
          </h2>
          {tumorDetected !== undefined && (
            <div
              className={`inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold mb-4 ${
                tumorDetected
                  ? "bg-red-100 text-red-800 border border-red-300"
                  : "bg-green-50 text-green-700 border border-green-200"
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
                    <dt className="text-gray-500 text-xs uppercase tracking-wider">
                      {field.label}
                    </dt>
                    <dd
                      className={`font-medium mt-0.5 ${
                        isVolumeField
                          ? "text-red-700 text-lg font-bold"
                          : "text-gray-900"
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
      {(() => {
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
            marked: "bg-red-100 text-red-800 border-red-300",
            severe: "bg-red-100 text-red-800 border-red-300",
            occluded: "bg-red-100 text-red-800 border-red-300",
            moderate: "bg-amber-100 text-amber-800 border-amber-300",
            mild: "bg-yellow-100 text-yellow-800 border-yellow-300",
            minimal: "bg-yellow-50 text-yellow-700 border-yellow-200",
          } as Record<string, string>)[sev || ""] ||
            "bg-gray-100 text-gray-700 border-gray-300");

        return (
          <div
            className="report-section"
            style={{ borderLeft: "4px solid #d97706", paddingLeft: "1.25rem" }}
          >
            <h2 className="report-section-title flex items-center gap-2">
              <AlertTriangle className="w-5 h-5 text-amber-600" />
              AI-Detected Abnormal Findings
            </h2>

            {lesionDetected && (
              <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-semibold mt-2 bg-red-100 text-red-800 border border-red-300">
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
                        <span className="font-semibold text-gray-900 capitalize">
                          {String(title).replace(/_/g, " ")}
                        </span>
                        {f.note && (
                          <p className="text-gray-600 mt-0.5 leading-relaxed">{f.note}</p>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}

            {segments.length > 0 && (
              <div className="mt-4">
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                  Per-Vessel Stenosis
                  {result.summary?.cad_rads != null && (
                    <span className="ml-2 normal-case text-gray-700">
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
                      <tr key={i} className={s.stenosis_pct >= 50 ? "bg-red-50" : ""}>
                        <td className="font-semibold text-gray-900">
                          {s.name || s.vessel || `Vessel ${i + 1}`}
                        </td>
                        <td className={s.stenosis_pct >= 50 ? "text-red-700 font-bold" : ""}>
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

            <p className="text-xs text-gray-400 mt-3 italic">
              AI screening output — not a diagnosis. Review against the images and clinical
              context.
            </p>
          </div>
        );
      })()}

      {/* Anatomical Region Summary (PET-CT: group lesions by region) */}
      {(() => {
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
                    <td className="font-semibold text-gray-900">{region}</td>
                    <td>{stats.count}</td>
                    <td className={stats.suvMax > 2.5 ? "text-red-700 font-bold" : ""}>{stats.suvMax.toFixed(2)}</td>
                    <td>{stats.mtv.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })()}

      {/* Measurements Table */}
      {uiSchema?.sections
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
                        className={hasVolume ? "bg-red-50" : ""}
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
                                    hasVolume ? "text-red-900" : "text-gray-900"
                                  }`
                                : col.key === "_value" && hasVolume
                                ? "text-red-700 font-bold"
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
                              hasVolume ? "text-red-600 font-semibold" : ""
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
                                <div className="flex-1 h-3 bg-gray-100 rounded-full overflow-hidden">
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
                <div className="mt-3 flex gap-6 text-xs text-gray-500">
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
          <div className="report-section">
            <h2 className="report-section-title">Fused PET/CT Images</h2>
            <p className="text-xs text-gray-400 mb-3">
              CT anatomy with PET SUV hot-colormap overlay. Only voxels above 20% of the display
              SUVmax are coloured to preserve CT anatomy in low-uptake regions. Scroll through every
              slice in each plane.
            </p>
            <div className="rounded-lg overflow-hidden border-2 border-gray-200">
              <FusedViewer
                studyUid={study.study_instance_uid}
                usecase={result.usecase_name}
                modes={["fused"]}
              />
            </div>
          </div>

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
                        <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5 text-center">
                          {label}
                        </div>
                        <div className="bg-black rounded-lg overflow-hidden border-2 border-gray-200">
                          <AuthImage
                            src={getArtifactUrl(study.study_instance_uid, result.usecase_name, artifact.name)}
                            alt={artifact.name}
                            className="w-full h-auto"
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
      ) : (
        /* ── MRI / non-PET: Segmentation overlay via preview endpoint ── */
        <div className="report-section">
          <h2 className="report-section-title">Imaging — Segmentation Overlay</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {VIEWS.map((view) => {
              const url = getPreviewUrl(study.study_instance_uid, result.usecase_name, view);
              return (
                <div key={view} className="relative group">
                  <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5 text-center">
                    {view}
                  </div>
                  <div
                    className="relative bg-black rounded-lg overflow-hidden cursor-pointer border-2 border-gray-200 hover:border-primary-400 transition-colors"
                    onClick={() => setZoomedView(view)}
                  >
                    <AuthImage
                      src={url}
                      alt={`${view} segmentation overlay`}
                      className="w-full h-auto"
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
          <p className="text-xs text-gray-400 mt-3 text-center">
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
          <div className="relative max-w-4xl max-h-[90vh] p-2">
            <button
              onClick={() => setZoomedView(null)}
              className="absolute -top-3 -right-3 bg-white rounded-full p-1.5 shadow-lg z-10 hover:bg-gray-100"
            >
              <X className="w-5 h-5 text-gray-700" />
            </button>
            <div className="text-center text-white text-sm font-medium mb-2 uppercase tracking-wider">
              {zoomedView} View
            </div>
            <AuthImage
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
      <div className="report-section">
        <h2 className="report-section-title">Quality Assurance</h2>
        <QAPanel flags={result.qa_flags} details={result.qa_details} />
      </div>

      {/* Segmentation Overlay */}
      {uiSchema?.sections
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
                      className="flex items-center gap-2 text-sm text-gray-600"
                    >
                      <Download className="w-3.5 h-3.5" />
                      <span>{a.name}</span>
                      <span className="text-xs text-gray-400">
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
        <div className="border-t border-gray-200 pt-3 text-xs text-gray-400 italic">
          DISCLAIMER: This is an AI-generated analysis intended to assist clinical
          decision-making. It is not a substitute for professional medical judgment.
          All findings must be reviewed and validated by a qualified radiologist or
          physician before clinical use.
        </div>
      </div>
    </div>
  );
}
