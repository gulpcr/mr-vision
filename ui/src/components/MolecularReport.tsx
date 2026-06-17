"use client";

import { Study, Result } from "@/lib/api";
import { Printer } from "lucide-react";

// Standalone formal departmental FDG PET-CT report (mirrors the downloadable
// PDF). Rendered on its own screen — NOT mixed into the generic ReportView.

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

const REPORT_INSTITUTION = "DEPARTMENT OF MOLECULAR IMAGING";
const REPORT_SIGNATORIES = ["Dr. Salman Habib", "Dr. Saifullah Sethar"];

function fmtAge(raw: string | null): string {
  if (!raw) return "—";
  const s = String(raw).trim();
  const m = s.match(/^(\d{3})([YMWD])$/i);
  if (m) {
    const unit = { Y: "Yrs", M: "Mos", W: "Wks", D: "Days" }[m[2].toUpperCase() as "Y" | "M" | "W" | "D"];
    return `${parseInt(m[1], 10)} ${unit}`;
  }
  return s;
}

function fmtSex(raw: string | null): string {
  if (!raw) return "—";
  return ({ M: "Male", F: "Female", O: "Other" } as Record<string, string>)[raw.trim().toUpperCase()] || raw;
}

function fmtReportDate(raw: string | null): string {
  if (!raw) return "—";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return raw;
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  return `${dd}/${mm}/${d.getFullYear()}`;
}

function lesionSentence(l: any): string {
  const region = l.anatomical_region || "the region";
  const parts = [`FDG-avid lesion in ${region}`];
  if (typeof l.suv_max === "number") parts.push(`with SUVmax ${l.suv_max.toFixed(1)}`);
  if (typeof l.volume_ml === "number") parts.push(`(metabolic volume ${l.volume_ml.toFixed(1)} mL)`);
  return parts.join(" ") + ".";
}

function buildConclusions(summary: any, lesions: any[]): string[] {
  const bullets: string[] = [];
  if (summary?.diagnosis) bullets.push(String(summary.diagnosis));
  if (lesions.length > 0) {
    const suv = typeof summary?.suvmax_body === "number" ? ` (highest SUVmax ${summary.suvmax_body.toFixed(1)})` : "";
    bullets.push(`${lesions.length} FDG-avid lesion(s) detected${suv}, consistent with metabolically active disease.`);
    if (summary?.deauville_score) bullets.push(`Deauville score: ${summary.deauville_score}.`);
    if (summary?.percist_score) bullets.push(`PERCIST status: ${summary.percist_score}.`);
    if (typeof summary?.mtv_total_ml === "number" && typeof summary?.tlg_total === "number")
      bullets.push(`Total metabolic tumour volume ${summary.mtv_total_ml.toFixed(1)} mL; total lesion glycolysis ${summary.tlg_total.toFixed(1)}.`);
  } else {
    bullets.push("No FDG-avid lesion suggestive of metabolically active disease was detected.");
  }
  return bullets;
}

interface MolecularReportProps {
  study: Study;
  result: Result;
}

export function MolecularReport({ study, result }: MolecularReportProps) {
  const summary: any = result.summary || {};
  const measurements: any = result.measurements || {};
  const lesions: any[] = Array.isArray(measurements.lesions) ? measurements.lesions : [];
  const isBrain = result.usecase_name === "pet_ct_brain";
  const coverage = isBrain ? "brain" : "vertex to mid-thigh";
  const tracer = summary.radiopharmaceutical || "18F-FDG";
  const liver = measurements?.reference_organs?.liver_suv_mean;

  const grouped: Record<string, any[]> = {};
  for (const l of lesions) {
    const sec = REGION_TO_SECTION[l.anatomical_region] || "ABDOMEN / PELVIS";
    (grouped[sec] ||= []).push(l);
  }

  const Field = ({ label, value }: { label: string; value: string }) => (
    <div className="flex gap-1">
      <span className="font-bold whitespace-nowrap">{label}:</span>
      <span>{value || "—"}</span>
    </div>
  );

  return (
    <div className="molecular-report mx-auto max-w-3xl bg-white text-gray-900">
      {/* Toolbar (hidden in print) */}
      <div className="no-print flex justify-end mb-3">
        <button
          onClick={() => window.print()}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-primary-900 text-white rounded-lg hover:bg-primary-800 transition-colors"
        >
          <Printer className="w-4 h-4" />
          Print
        </button>
      </div>

      <div className="border border-gray-300 rounded-lg p-8 text-sm leading-relaxed print:border-0 print:p-0">
        <h1 className="text-center text-lg font-bold tracking-wide mb-3">{REPORT_INSTITUTION}</h1>

        {/* Patient table */}
        <div className="grid grid-cols-3 gap-x-6 gap-y-1 border border-gray-800 rounded p-3 mb-4">
          <Field label="Name" value={study.patient_name || "—"} />
          <Field label="PRN" value={study.patient_id || "—"} />
          <Field label="Date" value={fmtReportDate(study.study_date)} />
          <Field label="Ref. Dr / Hosp" value={study.referring_physician || "—"} />
          <Field label="Age" value={fmtAge(study.patient_age)} />
          <Field label="Sex" value={fmtSex(study.patient_sex)} />
        </div>

        <p className="text-center font-bold mb-3">
          <sup>18</sup>F-FDG POSITRON EMISSION-COMPUTERIZED TOMOGRAPHY (FDG PET-CT)
        </p>

        <p className="mb-2 text-justify"><span className="font-bold">EXAMINATION:</span> {tracer} PET-CT scan, {coverage}.</p>
        <p className="mb-2 text-justify"><span className="font-bold">CLINICAL HISTORY:</span> [To be completed by referring clinician]</p>
        <p className="mb-2 text-justify"><span className="font-bold">COMPARATIVE STUDY:</span> No prior study available for comparison.</p>
        <p className="mb-2 text-justify">
          <span className="font-bold">PROCEDURE:</span> Approximately 60 minutes after the intravenous administration of {tracer},
          PET images were acquired from the {coverage} using 3-D acquisition. A low-dose CT was obtained for attenuation
          correction and anatomical localisation. Images were displayed in the axial, coronal and sagittal planes.
          Maximum Standardized Uptake Value (SUVmax) normalized for body weight was used.
        </p>

        <p className="font-bold mt-3 mb-1">TECHNIQUE:</p>
        <ul className="list-none pl-5 mb-3 space-y-0.5">
          <li>Height: {study.patient_height_cm ? `${study.patient_height_cm}` : "_____"} cm</li>
          <li>Weight: {study.patient_weight_kg ? `${study.patient_weight_kg}` : "_____"} kg</li>
          <li>Fasting blood sugar: _____ mg/dl</li>
          <li>Site of injection: _____</li>
          <li>Normal blood pool liver demonstrates SUVmax {typeof liver === "number" ? liver.toFixed(1) : "_____"}</li>
        </ul>

        <p className="font-bold mt-3 mb-1">SCAN FINDINGS:</p>
        {REPORT_SECTIONS.map(([name, fallback]) => {
          const secLesions = grouped[name] || [];
          const text = secLesions.length > 0 ? secLesions.map(lesionSentence).join(" ") : fallback;
          return (
            <p key={name} className="mb-2 text-justify">
              <span className="font-bold italic">{name}:</span> {text}
            </p>
          );
        })}

        <p className="font-bold mt-3 mb-1">CONCLUSIONS:</p>
        <ul className="list-disc pl-8 mb-4 space-y-1">
          {buildConclusions(summary, lesions).map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>

        <div className="flex justify-between mt-10 font-bold">
          <span>{REPORT_SIGNATORIES[0]}</span>
          <span>{REPORT_SIGNATORIES[1]}</span>
        </div>
      </div>
    </div>
  );
}
