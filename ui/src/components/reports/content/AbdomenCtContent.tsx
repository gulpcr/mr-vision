"use client";

import { useEffect, useState } from "react";
import { api, Study, Result, ClinicalForStudy } from "@/lib/api";
import { ctRegionMeta } from "@/lib/ctReport";
import { fmtAge, fmtSex, fmtDate } from "@/lib/reportFormat";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { AIProvenanceBanner } from "@/components/ui/AIProvenanceBanner";
import { ReportShell } from "@/components/reports/ReportShell";
import { Printer, FileDown } from "lucide-react";

// Formal CT / MRI narrative report content (CLINICAL FEATURES / FINDINGS /
// CONCLUSIONS) for the full CT-report-family use-case set (abdomen_ct + ct_*
// + all *_mri plugins — see lib/ctReport.ts's CT_REGION_META, which already
// covers all of them). FINDINGS/CONCLUSIONS come from the consolidated
// MedGemma report authored in the pipeline (summary.ai_report), which
// reorganizes the per-slice flags into grouped, non-redundant prose.
// NON-DIAGNOSTIC assistive output. Logic moved verbatim from the former
// AbdomenCtReport.tsx — only the surrounding chrome now goes through ReportShell.

const SIGNATORY_NAME = "Dr";
const SIGNATORY_TITLE = "Consultant Radiologist";
const SIGNATORY_QUALS = "MBBS, FCPS, M.Med";

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

interface AbdomenCtContentProps {
  study: Study;
  result: Result;
  onSignedOff?: () => void;
}

export function AbdomenCtContent({ study, result, onSignedOff }: AbdomenCtContentProps) {
  const summary: any = result.summary || {};
  const region = ctRegionMeta(result.usecase_name);
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
  // Only show text once the writer has responded (or failed) — never flash the raw
  // fallback and then swap it for the written report.
  const findingParas = reportLoading
    ? []
    : toParagraphs(
        (report?.findings || fallbackFindings) ||
          "No focal abnormality was flagged on the reviewed axial levels."
      );
  const conclusions: string = reportLoading
    ? ""
    : (report?.conclusions && report.conclusions.trim()) ||
      (aiReport.impression && String(aiReport.impression).trim()) ||
      (flagged.length ? "See findings above; correlation with clinical information advised." : "No acute focal abnormality flagged on the reviewed levels.");

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
    <ReportShell
      study={study}
      onSignedOff={onSignedOff}
      toolbar={
        <>
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
        </>
      }
      footerBanner={<AIProvenanceBanner variant="footer" className="mt-8" />}
      signatory={
        <div className="mt-16">
          <div className="border-t border-gray-700 w-64 mb-2" />
          <p className="font-bold italic">{SIGNATORY_NAME}</p>
          <p className="font-bold italic">{SIGNATORY_TITLE}</p>
          <p className="font-bold italic">{SIGNATORY_QUALS}</p>
        </div>
      }
      showComputerGeneratedNote
    >
      {/* Report status (reading workflow) */}
      <div className="flex justify-center mb-3">
        <StatusBadge
          variant="reading"
          status={rs}
          assignedTo={by}
          signedAt={study.signed_at ? fmtDate(study.signed_at) : null}
        />
      </div>

      {/* Demographics */}
      <div className="grid grid-cols-2 gap-x-10 gap-y-1.5 mb-5">
        <div><span className="font-bold">PATIENT :</span> {study.patient_name || "—"}</div>
        <div><span className="font-bold">MR :</span> {study.patient_id || "—"}</div>
        <div><span className="font-bold">DATE :</span> {fmtDate(study.study_date)}</div>
        <div><span className="font-bold">AGE :</span> {ageDisplay}</div>
        <div><span className="font-bold">GENDER :</span> {fmtSex(study.patient_sex || clinical?.sex || null)}</div>
        <div><span className="font-bold">REF :</span> {refDr}</div>
      </div>

      <p className="mt-2">
        <span className="font-bold underline">EXAMINATION:&nbsp;</span>
        <span className="font-bold">{region.examination}</span>
      </p>

      <p className="mt-3 text-justify">
        <span className="font-bold underline">TECHNIQUE:</span> {region.technique}
      </p>

      <Sec>CLINICAL FEATURES:</Sec>
      <p className="text-justify">{clinicalFeatures}</p>

      <Sec>FINDINGS:</Sec>
      {reportLoading ? (
        <p className="text-sm text-gray-400 italic mb-2 animate-pulse motion-reduce:animate-none">Writing the report from the AI findings…</p>
      ) : (
        findingParas.map((p, i) => (
          <p key={i} className="text-justify mb-2">{p}</p>
        ))
      )}

      <Sec>CONCLUSIONS:</Sec>
      {reportLoading ? (
        <p className="text-sm text-gray-400 italic animate-pulse motion-reduce:animate-none">…</p>
      ) : (
        <p className="text-justify">{conclusions}</p>
      )}
    </ReportShell>
  );
}
