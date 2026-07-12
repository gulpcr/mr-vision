"use client";

import { useEffect, useState } from "react";
import { api, Study, Result, ClinicalForStudy } from "@/lib/api";
import { Printer, FileDown } from "lucide-react";

// Standalone formal Abdomen CT report. Mirrors the MRI / PET-CT report layout: a
// clean, read-only rendered document with CLINICAL FEATURES / FINDINGS / CONCLUSIONS.
// FINDINGS and CONCLUSIONS come from the consolidated MedGemma report authored in the
// pipeline (summary.ai_report), which reorganizes the per-slice flags into grouped,
// non-redundant prose. NON-DIAGNOSTIC assistive output.

const SIGNATORY_NAME = "Dr. Ammar-e-Yasir";
const SIGNATORY_TITLE = "Consultant Radiologist";
const SIGNATORY_QUALS = "MBBS, FCPS, M.Med";
const EXAMINATION = "CT SCAN OF THE ABDOMEN AND PELVIS";
const TECHNIQUE_DEFAULT =
  "Axial CT images of the abdomen and pelvis were reviewed. Findings below are an AI-assisted read of soft-tissue-windowed axial levels sampled across the volume.";

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

// Strip the leading "[window]" provenance tag the scan pass prepends to each flag.
function cleanFinding(s: string): string {
  return String(s || "").replace(/^\s*\[[^\]]*\]\s*/, "").trim();
}

// Split the consolidated findings prose into paragraphs (double newline, else one).
function toParagraphs(text: string): string[] {
  const t = String(text || "").trim();
  if (!t) return [];
  const parts = t.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  return parts.length ? parts : [t];
}

export function AbdomenCtReport({ study, result }: { study: Study; result: Result }) {
  const summary: any = result.summary || {};
  const aiReport: any = summary.ai_report || {};
  const flagged: Array<{ z: number; finding: string }> = Array.isArray(summary.anomaly_findings)
    ? summary.anomaly_findings
    : [];

  // FINDINGS / CONCLUSIONS are written by a SEPARATE report-writer model that
  // reorganizes the pipeline's per-level flags (grounded, no new findings). We fetch
  // it from /reports/{uid}/{usecase}/consolidated-report; until it responds (or if it
  // fails) we fall back to the pipeline's own ai_report / raw flags.
  const [report, setReport] = useState<{ findings: string; conclusions: string; model: string | null } | null>(null);
  const [reportLoading, setReportLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setReportLoading(true);
    api.reports
      .consolidatedReport(study.study_instance_uid, result.usecase_name)
      .then((r) => { if (active) setReport(r); })
      .catch(() => { /* fall back to summary below */ })
      .finally(() => { if (active) setReportLoading(false); });
    return () => { active = false; };
  }, [study.study_instance_uid, result.usecase_name]);

  const fallbackFindings =
    (aiReport.findings && String(aiReport.findings).trim()) ||
    (flagged.length ? flagged.map((f) => `z=${f.z}: ${cleanFinding(f.finding)}`).join("\n") : "") ||
    (summary.processing_notes ? String(summary.processing_notes) : "");
  const findingParas = toParagraphs(
    (report?.findings || fallbackFindings) ||
      "No focal abnormality was flagged on the reviewed axial levels."
  );
  const conclusions: string =
    (report?.conclusions && report.conclusions.trim()) ||
    (aiReport.impression && String(aiReport.impression).trim()) ||
    (flagged.length ? "See findings above; correlation with clinical information advised." : "No acute focal abnormality flagged on the reviewed levels.");
  const disclaimer: string = aiReport.disclaimer || "";
  const provider: string = report?.model || summary.ai_report_provider || summary.medgemma_model || "";

  const [pdfLoading, setPdfLoading] = useState(false);

  // Clinical intake (patient onboarding) → Clinical Features.
  const [clinical, setClinical] = useState<ClinicalForStudy | null>(null);
  useEffect(() => {
    let active = true;
    api.onboarding
      .getClinical(study.study_instance_uid)
      .then((c) => { if (active) setClinical(c && Object.keys(c).length > 0 ? c : null); })
      .catch(() => { /* no order linked — keep placeholder */ });
    return () => { active = false; };
  }, [study.study_instance_uid]);

  const refDr = study.referring_physician || clinical?.referrer || "—";
  const ageDisplay = study.patient_age ? fmtAge(study.patient_age) : (clinical?.age_band || "—");
  const clinicalFeatures =
    (clinical?.clinical_history || clinical?.indication || "").trim() ||
    "No clinical history provided.";

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
      a.download = `abdomen_ct_report_${study.study_instance_uid.slice(0, 8)}.pdf`;
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
    <div className="abdomen-ct-report mx-auto max-w-3xl bg-white text-gray-900">
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

        {/* AI / non-diagnostic banner */}
        <div className="no-print mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-[12px] text-amber-800">
          AI-assisted report. An AI model reviewed axial CT levels and flagged potential
          abnormalities; the findings below were consolidated by a local language model.
          NON-DIAGNOSTIC — requires radiologist verification of the full study.
        </div>

        {/* Demographics */}
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
          <span className="font-bold">{EXAMINATION}</span>
        </p>

        <p className="mt-3 text-justify">
          <span className="font-bold underline">TECHNIQUE:</span> {TECHNIQUE_DEFAULT}
        </p>

        <Sec>CLINICAL FEATURES:</Sec>
        <p className="text-justify">{clinicalFeatures}</p>

        <Sec>FINDINGS:</Sec>
        {reportLoading && (
          <p className="no-print text-xs text-gray-400 mb-2">Writing report from the AI findings…</p>
        )}
        {findingParas.map((p, i) => (
          <p key={i} className="text-justify mb-2">{p}</p>
        ))}

        <Sec>CONCLUSIONS:</Sec>
        <p className="text-justify">{conclusions}</p>

        {disclaimer && (
          <p className="text-[11px] italic text-gray-500 mt-4">{disclaimer}</p>
        )}

        {/* Flagged levels appendix (transparency: the raw AI flags) */}
        {flagged.length > 0 && (
          <div className="mt-6">
            <p className="font-bold underline mb-1">AI-FLAGGED LEVELS (appendix):</p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="text-left text-gray-500 border-b border-gray-200">
                    <th className="py-1 pr-4 font-semibold">Level (z)</th>
                    <th className="py-1 font-semibold">Flagged finding</th>
                  </tr>
                </thead>
                <tbody>
                  {flagged.map((f, i) => (
                    <tr key={i} className="border-b border-gray-100">
                      <td className="py-1 pr-4 font-mono">z={f.z}</td>
                      <td className="py-1">{cleanFinding(f.finding)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[11px] italic text-gray-400 mt-1">
              Raw per-level flags from the AI scan{provider ? ` (${provider})` : ""} — the
              consolidated FINDINGS above are derived from these.
            </p>
          </div>
        )}

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
