"use client";

import { useEffect, useState } from "react";
import { api, Study, Result, ClinicalForStudy } from "@/lib/api";
import { Printer, FileDown } from "lucide-react";

// Standalone formal MRI narrative report (departmental brain MRI layout). Mirrors
// the PET-CT molecular report: a clean, read-only rendered document whose FINDINGS
// and IMPRESSION are auto-derived from the AI segmentation result. The downloadable
// PDF is generated server-side from the same result.

// Defaults mirror the backend Settings (config.py).
const SIGNATORY_NAME = "Dr. Ammar-e-Yasir";
const SIGNATORY_TITLE = "Consultant Radiologist";
const SIGNATORY_QUALS = "MBBS, FCPS, M.Med";
const TECHNIQUE_DEFAULT =
  "Multiplanar, multi-sequential MRI images of brain acquired with and without contrast.";

const EXAMINATION_BY_USECASE: Record<string, string> = {
  spine_mri: "MRI OF THE SPINE",
  chest_mri: "MRI OF THE CHEST",
  abdomen_mri: "MRI OF THE ABDOMEN",
  brain_mri: "MRI OF THE BRAIN PLAIN AND CONTRAST",
};

function fmtAge(raw: string | null): string {
  if (!raw) return "—";
  const m = String(raw).trim().match(/^(\d{3})([YMWD])$/i);
  if (m) {
    const unit = { Y: "Yrs", M: "Mos", W: "Wks", D: "Days" }[m[2].toUpperCase() as "Y" | "M" | "W" | "D"];
    return `${parseInt(m[1], 10)} ${unit}`;
  }
  return String(raw);
}
function fmtSex(raw: string | null): string {
  if (!raw) return "—";
  return ({ M: "Male", F: "Female", O: "Other" } as Record<string, string>)[raw.trim().toUpperCase()] || raw;
}
function fmtDate(raw: string | null): string {
  if (!raw) return "—";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return raw;
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}/${d.getFullYear()}`;
}

function humanize(s: string): string {
  return String(s).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const LOCATION_PHRASE: Record<string, string> = {
  left: "in the left cerebral hemisphere",
  right: "in the right cerebral hemisphere",
  midline: "near the midline",
};

// "2.1 × 1.9 × 1.5 cm (AP × TS × CC)" from the derived lesion_dimensions_cm.
function sizePhrase(summary: any): string {
  const dims = summary?.lesion_dimensions_cm || {};
  const { ap, transverse: ts, craniocaudal: cc } = dims;
  if ([ap, ts, cc].every((v) => typeof v === "number")) {
    return `${ap.toFixed(1)} × ${ts.toFixed(1)} × ${cc.toFixed(1)} cm (AP × TS × CC)`;
  }
  const ordered = Object.values(dims).filter((v) => typeof v === "number") as number[];
  return ordered.length === 3 ? `${ordered.map((v) => v.toFixed(1)).join(" × ")} cm` : "";
}

// "T2 and FLAIR hyperintense, T1 hypointense relative to brain parenchyma."
function signalPhrase(summary: any): string {
  const signal: Record<string, string> = summary?.signal_profile || {};
  const mods = Object.keys(signal);
  if (!mods.length) return "";
  const byDesc: Record<string, string[]> = {};
  for (const m of mods) (byDesc[signal[m]] ||= []).push(m);
  const clauses = (["hyperintense", "hypointense", "isointense"] as const)
    .filter((d) => byDesc[d]?.length)
    .map((d) => `${byDesc[d].join(" and ")} ${d}`);
  return clauses.length
    ? `The lesion appears ${clauses.join(", ")} relative to surrounding brain parenchyma.`
    : "";
}

// Auto-derive FINDINGS paragraphs from the MRI AI segmentation result. Mirrors how
// the PET-CT report builds SCAN FINDINGS from the result, enriched with lesion
// geometry (size/location/count) and relative signal characterisation.
function buildFindings(summary: any, measurements: any): string[] {
  const lines: string[] = [];
  const volumes: Record<string, any> = measurements?.volumes_ml || {};
  const detected = summary?.tumor_detected === true || summary?.lesion_detected === true;

  if (detected) {
    const total = summary?.total_lesion_volume_ml;
    const count = summary?.lesion_count;
    const location = summary?.lesion_location;
    const size = sizePhrase(summary);

    let lead =
      typeof count === "number" && count > 1
        ? `AI segmentation identifies ${count} segmented lesions; the largest`
        : "AI segmentation identifies a lesion";
    if (LOCATION_PHRASE[location]) lead += ` ${LOCATION_PHRASE[location]}`;
    if (size) lead += `, measuring approximately ${size}`;
    if (typeof total === "number") lead += `, with a total segmented volume of ${total.toFixed(1)} mL`;
    lines.push(lead.replace(/,\s*$/, "") + ".");

    const sig = signalPhrase(summary);
    if (sig) lines.push(sig);

    const comps = Object.entries(volumes).filter(
      ([, v]) => typeof v === "number" && (v as number) > 0
    );
    if (comps.length) {
      const parts = comps.map(([k, v]) => `${humanize(k)} ${(v as number).toFixed(1)} mL`);
      lines.push(`Segmented component volumes — ${parts.join("; ")}.`);
    }
  } else {
    lines.push(
      "No abnormal segmented lesion was detected on the analysed sequences by the AI model."
    );
  }

  const findings = Array.isArray(summary?.abnormal_findings) ? summary.abnormal_findings : [];
  for (const f of findings) {
    if (!f || typeof f !== "object") continue;
    const title = humanize(String(f.organ || f.finding || f.side || "Finding"));
    const sev = f.severity || f.status || "";
    const note = f.note || "";
    let line = title;
    if (sev) line += ` — ${humanize(String(sev))}`;
    if (note) line += `: ${note}`;
    lines.push(line);
  }

  if (summary?.processing_notes) lines.push(String(summary.processing_notes));
  return lines;
}

function buildImpression(summary: any): string {
  const detected = summary?.tumor_detected === true || summary?.lesion_detected === true;
  if (detected) {
    const total = summary?.total_lesion_volume_ml;
    const location = summary?.lesion_location;
    const size = sizePhrase(summary);
    const sizeTxt = size
      ? ` measuring ~${size}`
      : typeof total === "number" ? ` (~${total.toFixed(1)} mL)` : "";
    const loc = LOCATION_PHRASE[location] ? ` ${LOCATION_PHRASE[location]}` : "";
    return `Segmented brain lesion${sizeTxt}${loc} identified on AI analysis — recommend clinical and radiological correlation.`;
  }
  return "No segmentable focal lesion identified on AI analysis.";
}

export function MriReport({ study, result }: { study: Study; result: Result }) {
  const summary: any = result.summary || {};
  const measurements: any = result.measurements || {};
  const examination =
    EXAMINATION_BY_USECASE[result.usecase_name] || "MRI OF THE BRAIN PLAIN AND CONTRAST";
  const findings = buildFindings(summary, measurements);
  const impression = buildImpression(summary);

  const [pdfLoading, setPdfLoading] = useState(false);

  // Clinical intake (patient onboarding) — fills the clinical indication + fallbacks.
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
  const clinicalIndication =
    clinical?.clinical_history || clinical?.indication || "";

  // Reading / report status (radiologist workflow).
  const rs = study.reading_status || "unread";
  const by = study.assigned_to_username;
  const statusBadge = (
    {
      unread: { label: "Unclaimed", cls: "bg-gray-100 text-gray-600 border-gray-300" },
      in_progress: { label: by ? `Reading — ${by}` : "Reading", cls: "bg-blue-100 text-blue-700 border-blue-300" },
      reported: { label: by ? `Reported — ${by}` : "Reported", cls: "bg-amber-100 text-amber-800 border-amber-300" },
      signed: {
        label: `Signed off${by ? ` — ${by}` : ""}${study.signed_at ? ` · ${fmtDate(study.signed_at)}` : ""}`,
        cls: "bg-green-100 text-green-700 border-green-400",
      },
    } as Record<string, { label: string; cls: string }>
  )[rs] || { label: rs, cls: "bg-gray-100 text-gray-600 border-gray-300" };
  const isPreliminary = rs !== "signed";

  const downloadPdf = async () => {
    setPdfLoading(true);
    try {
      const blob = await api.reports.downloadPdfBlob(study.study_instance_uid, result.usecase_name);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `mri_report_${study.study_instance_uid.slice(0, 8)}.pdf`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } finally {
      setPdfLoading(false);
    }
  };

  const Sec = ({ children }: { children: React.ReactNode }) => (
    <p className="font-bold underline mt-4 mb-1">{children}</p>
  );

  return (
    <div className="mri-report mx-auto max-w-3xl bg-white text-gray-900">
      {/* Toolbar (hidden in print) */}
      <div className="no-print flex justify-end gap-2 mb-3">
        <button
          onClick={downloadPdf}
          disabled={pdfLoading}
          className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          <FileDown className="w-4 h-4" /> {pdfLoading ? "Generating…" : "Download PDF"}
        </button>
        <button
          onClick={() => window.print()}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-primary-900 text-white rounded-lg hover:bg-primary-800 transition-colors"
        >
          <Printer className="w-4 h-4" /> Print
        </button>
      </div>

      <div className="border border-gray-300 rounded-lg p-8 text-sm leading-relaxed print:border-0 print:p-0">
        {/* Report status (reading workflow) */}
        <div className="flex justify-center mb-3">
          <span className={`px-3 py-1 rounded-full text-xs font-bold border ${statusBadge.cls}`}>
            {isPreliminary ? "PRELIMINARY · " : ""}{statusBadge.label}
          </span>
        </div>

        {/* Demographics (two columns, label : value) */}
        <div className="grid grid-cols-2 gap-x-10 gap-y-1.5 mb-5">
          <div><span className="font-bold">PATIENT :</span> {study.patient_name || "—"}</div>
          <div><span className="font-bold">MR :</span> {study.patient_id || "—"}</div>
          <div><span className="font-bold">DATE :</span> {fmtDate(study.study_date)}</div>
          <div><span className="font-bold">AGE :</span> {ageDisplay}</div>
          <div><span className="font-bold">GENDER :</span> {fmtSex(study.patient_sex)}</div>
          <div><span className="font-bold">REF :</span> {refDr}</div>
        </div>

        <p className="mt-2">
          <span className="font-bold underline">EXAMINATION:&nbsp;</span>
          <span className="font-bold">{examination}</span>
        </p>

        <p className="mt-3 text-justify">
          <span className="font-bold underline">TECHNIQUE:</span> {TECHNIQUE_DEFAULT}
        </p>

        <Sec>CLINICAL INDICATION:</Sec>
        <p className="text-justify">{clinicalIndication || "—"}</p>

        <Sec>FINDINGS:</Sec>
        {findings.map((p, i) => (
          <p key={i} className="text-justify mb-2">{p}</p>
        ))}

        <Sec>IMPRESSION:</Sec>
        <p className="text-justify">{impression}</p>

        {/* Signatory */}
        <div className="mt-16">
          <div className="border-t border-gray-700 w-64 mb-2" />
          <p className="font-bold italic">{SIGNATORY_NAME}</p>
          <p className="font-bold italic">{SIGNATORY_TITLE}</p>
          <p className="font-bold italic">{SIGNATORY_QUALS}</p>
        </div>

        <p className="text-center text-[11px] italic font-bold text-gray-500 mt-10 pt-3 border-t border-gray-100">
          Note: This is a computer generated document and does not require any signature.
        </p>
      </div>
    </div>
  );
}
