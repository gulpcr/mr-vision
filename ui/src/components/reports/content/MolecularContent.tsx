"use client";

import { useEffect, useState } from "react";
import { api, Study, Result, ClinicalForStudy } from "@/lib/api";
import { fmtAge, fmtSex, fmtDate as fmtReportDate } from "@/lib/reportFormat";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { AIProvenanceBanner } from "@/components/ui/AIProvenanceBanner";
import { ReportShell } from "@/components/reports/ReportShell";
import { Printer } from "lucide-react";

// Standalone formal departmental FDG PET-CT report (mirrors the downloadable
// PDF). Logic moved verbatim from the former MolecularReport.tsx — only the
// surrounding chrome now goes through ReportShell. No PDF download exists for
// this use case today (print only) — preserved as-is, not added.

const REGION_TO_SECTION: Record<string, string> = {
  Brain: "HEAD & NECK",
  "Head/Neck": "HEAD & NECK",
  Thorax: "THORAX",
  "Upper Abdomen": "ABDOMEN / PELVIS",
  "Lower Abdomen/Pelvis": "ABDOMEN / PELVIS",
  "Pelvis/Perineum": "ABDOMEN / PELVIS",
};

const REPORT_SECTIONS: [string, string][] = [
  ["HEAD & NECK", "No abnormal FDG-avid lesion is seen in the head and neck region. Physiological FDG activity is noted in the brain."],
  ["THORAX", "No FDG-avid lesion is seen in the thorax. Physiological FDG uptake is noted in the myocardium and great vessels."],
  ["ABDOMEN / PELVIS", "No hypermetabolic lesion is seen in this region. The liver, spleen, pancreas and bowel show physiological tracer distribution."],
  ["BONES / BONE MARROW", "No FDG-avid / non-avid skeletal lesion is noted in this region."],
];

// Maps each report heading onto the fixed key PetCtNarrativeService's Gemini prompt
// returns (backend/app/application/pet_ct_narrative_service.py) — mirrors
// pdf_generator._REPORT_SECTION_TO_AI_KEY so this view and the PDF agree.
const REPORT_SECTION_TO_AI_KEY: Record<string, string> = {
  "HEAD & NECK": "head_neck",
  THORAX: "thorax",
  "ABDOMEN / PELVIS": "abdomen_pelvis",
  "BONES / BONE MARROW": "bones_marrow",
};

const REPORT_INSTITUTION = "DEPARTMENT OF MOLECULAR IMAGING";
const REPORT_SIGNATORIES = ["Dr. Salman Habib", "Dr. Saifullah Sethar"];

// TotalSegmentator label → human-readable structure (mirrors pdf_generator.py).
function prettifyStructure(name: string | null | undefined): string {
  if (!name) return "soft-tissue site";
  let n = name;
  let side = "";
  for (const suf of ["_left", "_right"]) {
    if (n.endsWith(suf)) { side = suf.slice(1) + " "; n = n.slice(0, -suf.length); break; }
  }
  if (n.startsWith("vertebrae_")) return `${side}${n.split("_")[1]} vertebra`.trim();
  return `${side}${n.replace(/_/g, " ")}`.trim();
}

// TotalSegmentator skeletal-muscle labels. A muscle is not a reportable organ,
// so muscle foci are named by region ("soft-tissue site"), not by muscle name.
// Mirrors pdf_generator._MUSCLE_BASENAMES / _is_muscle_structure.
const MUSCLE_BASENAMES = new Set([
  "iliopsoas", "gluteus_maximus", "gluteus_medius", "gluteus_minimus",
  "autochthon",
]);

function isMuscleStructure(name: string | null | undefined): boolean {
  if (!name) return false;
  let base = name;
  for (const suf of ["_left", "_right"]) {
    if (base.endsWith(suf)) { base = base.slice(0, -suf.length); break; }
  }
  return MUSCLE_BASENAMES.has(base);
}

// One sentence per anatomical structure (dominant SUVmax, focus count, size),
// instead of one sentence per focus. Physiologic/excretory foci summarised
// separately. Mirrors pdf_generator._aggregate_section_findings.
function aggregateSectionFindings(secLesions: any[]): string {
  const disease = secLesions.filter((l) => !l.physiologic_uptake);
  const physiologic = secLesions.filter((l) => l.physiologic_uptake);

  const groups: Record<string, any[]> = {};
  for (const l of disease) {
    const struct = isMuscleStructure(l.structure) ? "" : (l.structure || "");
    (groups[struct] ||= []).push(l);
  }

  const peak = (foci: any[]) => Math.max(...foci.map((x) => (typeof x.suv_max === "number" ? x.suv_max : 0)));
  const keys = Object.keys(groups).sort((a, b) => peak(groups[b]) - peak(groups[a]));

  const sizeStr = (l: any): string => {
    const d = l?.dimensions_cm;
    if (!Array.isArray(d) || d.length < 1) return "";
    const src = l.size_source === "ct" ? "CT" : "PET extent";
    return `${d.map((x: number) => x.toFixed(1)).join(" × ")} cm (${src})`;
  };

  const parts: string[] = [];
  for (const key of keys) {
    const foci = groups[key];
    const label = key ? prettifyStructure(key) : "soft-tissue site";
    const lead = label.charAt(0).toUpperCase() + label.slice(1);
    const suvs = foci.map((x) => x.suv_max).filter((v) => typeof v === "number");
    const vols = foci.map((x) => x.volume_ml).filter((v) => typeof v === "number");
    const dominant = foci.reduce((a, b) => ((b.suv_max || 0) > (a.suv_max || 0) ? b : a), foci[0]);
    const size = sizeStr(dominant);
    let s: string;
    if (foci.length === 1) {
      s = `${lead}: FDG-avid focus`;
      if (suvs.length) s += ` with SUVmax ${Math.max(...suvs).toFixed(1)}`;
      if (size) s += `, ${size}`;
      if (vols.length) s += ` (metabolic volume ${(vols[0] * 1000).toFixed(0)} mm³)`;
    } else {
      s = `${lead}: ${foci.length} FDG-avid foci`;
      if (suvs.length) s += `, most avid SUVmax ${Math.max(...suvs).toFixed(1)}`;
      if (size) s += ` (${size})`;
      if (vols.length) s += `, largest ${(Math.max(...vols) * 1000).toFixed(0)} mm³`;
    }
    parts.push(s + ".");
  }
  if (physiologic.length) {
    const sites = Array.from(new Set(physiologic.map((l) => prettifyStructure(l.structure)))).sort();
    parts.push(
      `Note: ${physiologic.length} focus/foci localise to ${sites.join(", ")} — ` +
      "pattern of physiologic / excretory uptake, not reported as disease."
    );
  }
  return parts.length ? parts.join(" ") : "No FDG-avid lesion is seen in this region.";
}

function buildConclusions(summary: any, lesions: any[]): string[] {
  const bullets: string[] = [];
  if (summary?.diagnosis) bullets.push(String(summary.diagnosis));
  if (lesions.length > 0) {
    const suv = typeof summary?.suvmax_body === "number" ? ` (highest SUVmax ${summary.suvmax_body.toFixed(1)})` : "";
    bullets.push(`${lesions.length} FDG-avid lesion(s) detected${suv}, consistent with metabolically active disease.`);
    if (summary?.deauville_score) bullets.push(`Deauville score: ${summary.deauville_score}.`);
    if (typeof summary?.tumor_to_liver_ratio === "number")
      bullets.push(`Tumor-to-liver ratio (SUVmax/liver SUVmean): ${summary.tumor_to_liver_ratio.toFixed(2)}.`);
    if (summary?.percist_score) bullets.push(`PERCIST status: ${summary.percist_score}.`);
    if (typeof summary?.mtv_total_ml === "number" && typeof summary?.tlg_total === "number")
      bullets.push(`Total metabolic tumour volume ${(summary.mtv_total_ml * 1000).toFixed(0)} mm³; total lesion glycolysis ${summary.tlg_total.toFixed(1)}.`);
  } else {
    bullets.push("No FDG-avid lesion suggestive of metabolically active disease was detected.");
  }
  return bullets;
}

interface MolecularContentProps {
  study: Study;
  result: Result;
  onSignedOff?: () => void;
}

export function MolecularContent({ study, result, onSignedOff }: MolecularContentProps) {
  const summary: any = result.summary || {};
  const measurements: any = result.measurements || {};
  const lesions: any[] = Array.isArray(measurements.lesions) ? measurements.lesions : [];
  const isBrain = result.usecase_name === "pet_ct_brain";
  const coverage = isBrain ? "brain" : "vertex to mid-thigh";
  const tracer = summary.radiopharmaceutical || "18F-FDG";
  const liver = measurements?.reference_organs?.liver_suv_mean;

  // Clinical intake (patient onboarding) — fills the report's clinical fields.
  const [clinical, setClinical] = useState<ClinicalForStudy | null>(null);
  useEffect(() => {
    let active = true;
    api.onboarding
      .getClinical(study.study_instance_uid)
      .then((c) => { if (active) setClinical(c && Object.keys(c).length > 0 ? c : null); })
      .catch(() => { /* no order linked — keep placeholders */ });
    return () => { active = false; };
  }, [study.study_instance_uid]);

  const refDr = study.referring_physician || clinical?.referrer || "—";
  const ageDisplay = study.patient_age ? fmtAge(study.patient_age) : (clinical?.age_band || "—");
  const clinicalHistory = clinical?.clinical_history || clinical?.indication || "[To be completed by referring clinician]";
  const comparative = clinical?.comparative_study || "No prior study available for comparison.";
  const heightVal = study.patient_height_cm ?? clinical?.height_cm ?? null;
  const weightVal = study.patient_weight_kg ?? clinical?.weight_kg ?? null;
  // BMI is derived from height/weight; prefer the backend-computed value, else compute locally.
  const bmiVal = clinical?.bmi ?? (
    heightVal && weightVal && heightVal > 0
      ? Math.round((weightVal / ((heightVal / 100) ** 2)) * 10) / 10
      : null
  );

  // Reading / report status (radiologist workflow) — shown on the report.
  const rs = study.reading_status || "unread";
  const by = study.assigned_to_username;

  const grouped: Record<string, any[]> = {};
  for (const l of lesions) {
    const sec = REGION_TO_SECTION[l.anatomical_region] || "ABDOMEN / PELVIS";
    (grouped[sec] ||= []).push(l);
  }

  // ai_report (Result.summary.ai_report) holds Gemini's image-read findings,
  // generated once during the pipeline from the MIP/fused PNGs + this same lesion
  // list (see tasks.py / pet_ct_narrative_service.py). Falls back per-section to
  // the deterministic aggregator when absent — mirrors pdf_generator.py exactly.
  const aiReport = summary.ai_report && typeof summary.ai_report === "object" ? summary.ai_report : null;
  const aiScanFindings: Record<string, string> = aiReport?.scan_findings || {};
  const aiConclusions: string[] | null = Array.isArray(aiReport?.conclusions) ? aiReport.conclusions : null;

  const lowConfidence = summary.confidence === "low";
  const confidenceReasons: string[] = Array.isArray(summary.confidence_reasons)
    ? summary.confidence_reasons
    : [];

  const Field = ({ label, value }: { label: string; value: string }) => (
    <div className="flex gap-1">
      <span className="font-bold whitespace-nowrap">{label}:</span>
      <span>{value || "—"}</span>
    </div>
  );

  return (
    <ReportShell
      study={study}
      onSignedOff={onSignedOff}
      toolbar={
        <button
          onClick={() => window.print()}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-primary-900 text-white rounded-lg hover:bg-primary-800 transition-colors"
        >
          <Printer className="w-4 h-4" />
          Print
        </button>
      }
      footerBanner={<AIProvenanceBanner variant="footer" additionalNote={aiReport?.disclaimer} className="mb-4" />}
      signatory={
        <div className="flex justify-between mt-10 font-bold">
          <span>{REPORT_SIGNATORIES[0]}</span>
          <span>{REPORT_SIGNATORIES[1]}</span>
        </div>
      }
    >
      <h1 className="text-center text-lg font-bold tracking-wide mb-2">{REPORT_INSTITUTION}</h1>

      {/* Report status (reading workflow) */}
      <div className="flex justify-center mb-3">
        <StatusBadge
          variant="reading"
          status={rs}
          assignedTo={by}
          signedAt={study.signed_at ? fmtReportDate(study.signed_at) : null}
        />
      </div>

      {lowConfidence && (
        <div className="mb-4 rounded-md border-2 border-amber-500 bg-amber-50 px-4 py-3 print:border print:border-black">
          <p className="font-bold text-amber-900 uppercase tracking-wide text-[13px]">
            ⚠ Low-confidence result — interpret with caution
          </p>
          {summary.quantitative === false && (
            <p className="text-amber-900 mt-1">
              Quantitative SUV could not be calibrated; uptake values are relative,
              not absolute SUV.
            </p>
          )}
          {confidenceReasons.length > 0 && (
            <ul className="list-disc pl-6 mt-1 text-amber-900 space-y-0.5">
              {confidenceReasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Patient table */}
      <div className="grid grid-cols-3 gap-x-6 gap-y-1 border border-gray-800 rounded p-3 mb-4">
        <Field label="Name" value={study.patient_name || "—"} />
        <Field label="PRN" value={study.patient_id || "—"} />
        <Field label="Date" value={fmtReportDate(study.study_date)} />
        <Field label="Ref. Dr / Hosp" value={refDr} />
        <Field label="Age" value={ageDisplay} />
        <Field label="Sex" value={fmtSex(study.patient_sex)} />
      </div>

      <p className="text-center font-bold mb-3">
        <sup>18</sup>F-FDG POSITRON EMISSION-COMPUTERIZED TOMOGRAPHY (FDG PET-CT)
      </p>

      <p className="mb-2 text-justify"><span className="font-bold">EXAMINATION:</span> {tracer} PET-CT scan, {coverage}.</p>
      <p className="mb-2 text-justify"><span className="font-bold">CLINICAL HISTORY:</span> {clinicalHistory}</p>
      <p className="mb-2 text-justify"><span className="font-bold">COMPARATIVE STUDY:</span> {comparative}</p>
      <p className="mb-2 text-justify">
        <span className="font-bold">PROCEDURE:</span> Approximately 60 minutes after the intravenous administration of {tracer},
        PET images were acquired from the {coverage} using 3-D acquisition. A low-dose CT was obtained for attenuation
        correction and anatomical localisation. Images were displayed in the axial, coronal and sagittal planes.
        Maximum Standardized Uptake Value (SUVmax) normalized for body weight was used.
      </p>

      <p className="font-bold mt-3 mb-1">TECHNIQUE:</p>
      <ul className="list-none pl-5 mb-3 space-y-0.5">
        <li>Height: {heightVal != null ? `${heightVal}` : "_____"} cm</li>
        <li>Weight: {weightVal != null ? `${weightVal}` : "_____"} kg</li>
        <li>BMI: {bmiVal != null ? `${bmiVal}` : "_____"} kg/m²</li>
        <li>Fasting blood sugar: {clinical?.fasting_glucose || "_____"} mg/dl</li>
        <li>Serum creatinine: {clinical?.creatinine || "_____"} mg/dl</li>
        <li>Site of injection: {clinical?.injection_site || "_____"}</li>
        <li>Normal blood pool liver demonstrates SUVmean {typeof liver === "number" ? liver.toFixed(2) : "_____"}</li>
      </ul>

      <p className="font-bold mt-3 mb-1">SCAN FINDINGS:</p>
      {REPORT_SECTIONS.map(([name, fallback]) => {
        const aiText = aiScanFindings[REPORT_SECTION_TO_AI_KEY[name]];
        let text = aiText;
        if (!text) {
          const secLesions = grouped[name] || [];
          text = secLesions.length > 0 ? aggregateSectionFindings(secLesions) : fallback;
        }
        return (
          <p key={name} className="mb-2 text-justify">
            <span className="font-bold italic">{name}:</span> {text}
          </p>
        );
      })}

      <p className="font-bold mt-3 mb-1">CONCLUSIONS:</p>
      <ul className="list-disc pl-8 mb-4 space-y-1">
        {(aiConclusions || buildConclusions(summary, lesions)).map((b, i) => (
          <li key={i}>{b}</li>
        ))}
      </ul>

      {/* Appendix: raw AI-detected foci — printed regardless of whether ai_report
          is present, so the (AI-based, not ground-truth) detections stay
          auditable against the prose above. Mirrors pdf_generator.py. */}
      {lesions.length > 0 && (
        <>
          <p className="font-bold mt-3 mb-1">APPENDIX: AI-DETECTED FOCI (for radiologist cross-reference)</p>
          <table className="w-full text-xs border border-gray-400 mb-4">
            <thead>
              <tr className="bg-gray-100">
                <th className="border border-gray-400 px-2 py-1 text-left">#</th>
                <th className="border border-gray-400 px-2 py-1 text-left">Region</th>
                <th className="border border-gray-400 px-2 py-1 text-left">Structure</th>
                <th className="border border-gray-400 px-2 py-1 text-left">SUVmax</th>
                <th className="border border-gray-400 px-2 py-1 text-left">Vol (mL)</th>
                <th className="border border-gray-400 px-2 py-1 text-left">CT (HU)</th>
              </tr>
            </thead>
            <tbody>
              {[...lesions]
                .sort((a, b) => (a.id ?? 0) - (b.id ?? 0))
                .map((le, i) => (
                  <tr key={i}>
                    <td className="border border-gray-400 px-2 py-1">{le.id ?? ""}</td>
                    <td className="border border-gray-400 px-2 py-1">{le.anatomical_region || "—"}</td>
                    <td className="border border-gray-400 px-2 py-1">{prettifyStructure(le.structure)}</td>
                    <td className="border border-gray-400 px-2 py-1">
                      {typeof le.suv_max === "number" ? le.suv_max.toFixed(1) : "—"}
                    </td>
                    <td className="border border-gray-400 px-2 py-1">
                      {typeof le.volume_ml === "number" ? le.volume_ml.toFixed(1) : "—"}
                    </td>
                    <td className="border border-gray-400 px-2 py-1">
                      {typeof le.ct_mean_hu === "number" ? le.ct_mean_hu.toFixed(0) : "—"}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      )}
    </ReportShell>
  );
}
