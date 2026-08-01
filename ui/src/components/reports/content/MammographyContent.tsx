"use client";

import { useState } from "react";
import { api, Study, Result } from "@/lib/api";
import { Printer, FileDown, AlertTriangle, X } from "lucide-react";
import { fmtAge, fmtSex, fmtDate } from "@/lib/reportFormat";
import { formatPatientName } from "@/lib/format";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { AIProvenanceBanner } from "@/components/ui/AIProvenanceBanner";
import { ReportShell } from "@/components/reports/ReportShell";

// Read-only, AI-generated bilateral mammography report (AECH-KIRAN layout).
// The definitive report is written by Gemini from the rendered views + the fine-tuned
// model's per-breast findings + clinical context (see MAMMOGRAPHY_AI_REPORT_ENABLED).
// It is NON-DIAGNOSTIC and rendered read-only; the PDF is generated from the same AI result.
// Logic moved verbatim from the former MammographyReport.tsx.

const HOSPITAL_NAME = "AECH-KIRAN";
const HOSPITAL_SUBTITLE =
  "Atomic Energy Cancer Hospital — Karachi Institute of Radiotherapy and Nuclear Medicine (KIRAN)";

interface MammographyContentProps {
  study: Study;
  result: Result | null;
  onSignedOff?: () => void;
}

export function MammographyContent({ study, result, onSignedOff }: MammographyContentProps) {
  const [pdfLoading, setPdfLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const summary: any = result?.summary || {};
  const laterality = ((summary.laterality as string) || "bilateral").toLowerCase();
  const showRight = laterality !== "left";
  const showLeft = laterality !== "right";
  const scope = laterality === "right" ? "of the right breast " : laterality === "left" ? "of the left breast " : "of both breasts ";
  const title = laterality === "right" ? "RIGHT MAMMOGRAPHY" : laterality === "left" ? "LEFT MAMMOGRAPHY" : "BILATERAL MAMMOGRAPHY";

  const rs = study.reading_status || "unread";
  const by = study.assigned_to_username;

  const downloadPdf = async () => {
    setPdfLoading(true); setError(null);
    try {
      const blob = await api.mammography.downloadPdfBlob(study.study_instance_uid);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `mammography_${study.study_instance_uid.slice(0, 8)}.pdf`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e.message || "PDF generation failed");
    } finally {
      setPdfLoading(false);
    }
  };

  const hasReport = summary.right_breast_findings || summary.left_breast_findings || summary.opinion;
  const Head = ({ children }: { children: React.ReactNode }) => (
    <p className="font-bold text-sm mt-4 mb-1">{children}</p>
  );
  const Prose = ({ text }: { text?: string }) => (
    <p className="text-sm leading-relaxed whitespace-pre-line text-gray-900 dark:text-gray-100">{text || "—"}</p>
  );

  return (
    <ReportShell
      study={study}
      onSignedOff={onSignedOff}
      containerClassName="bg-white dark:bg-surface border border-gray-300 dark:border-gray-700 rounded-lg p-8 text-sm leading-relaxed text-gray-900 dark:text-gray-100 print:border-0 print:p-0"
      toolbar={
        <>
          <button onClick={downloadPdf} disabled={pdfLoading}
            className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50">
            <FileDown className="w-4 h-4" /> {pdfLoading ? "Generating…" : "Download PDF"}
          </button>
          <button onClick={() => window.print()}
            className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50">
            <Printer className="w-4 h-4" /> Print
          </button>
        </>
      }
      topBanner={
        <>
          {error && (
            <div className="no-print flex items-start gap-3 bg-red-50 border border-red-200 rounded-lg px-4 py-2.5 mb-3">
              <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
              <p className="text-sm text-red-700 flex-1">{error}</p>
              <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600"><X className="w-4 h-4" /></button>
            </div>
          )}
          <AIProvenanceBanner
            variant="callout"
            additionalNote="Written by Gemini from the mammogram images and the AI model's per-breast findings. NON-DIAGNOSTIC."
            className="mb-3"
          />
        </>
      }
    >
      {/* Report status (reading workflow) — new for this use case, matching the
          other 3 reports' existing chrome (this report previously had none). */}
      <div className="flex justify-center mb-3">
        <StatusBadge
          variant="reading"
          status={rs}
          assignedTo={by}
          signedAt={study.signed_at ? fmtDate(study.signed_at) : null}
        />
      </div>

      {/* Hospital header */}
      <h1 className="text-center text-xl font-bold tracking-wide">{HOSPITAL_NAME}</h1>
      <p className="text-center text-xs text-gray-500 mb-4">{HOSPITAL_SUBTITLE}</p>

      {/* Patient table */}
      <div className="grid grid-cols-3 gap-x-6 gap-y-2 border border-gray-800 rounded p-3 mb-3">
        <div><span className="font-bold">PRN:</span> {study.patient_id || "—"}</div>
        <div><span className="font-bold">File No.:</span> NIL</div>
        <div><span className="font-bold">Status:</span> —</div>
        <div><span className="font-bold">Name:</span> {study.patient_name ? formatPatientName(study.patient_name) : "—"}</div>
        <div><span className="font-bold">Age/Gender:</span> {fmtAge(study.patient_age)} / {fmtSex(study.patient_sex)}</div>
        <div><span className="font-bold">Contact:</span> —</div>
        <div><span className="font-bold">Entry Date:</span> {fmtDate(study.study_date)}</div>
      </div>

      <p className="text-center font-bold text-base my-3">{title}</p>

      {!hasReport ? (
        <p className="text-sm text-gray-500 py-6 text-center">
          No AI report is available for this study yet (it is generated when the mammography pipeline runs
          with AI reporting enabled).
        </p>
      ) : (
        <>
          <Head>Procedure:</Head>
          <Prose text={`Digital mammography ${scope}performed in routine CC and MLO views.`} />

          <Head>Clinical Features:</Head>
          <Prose text={summary.clinical_features} />

          <Head>Findings:</Head>
          {showRight && (
            <>
              <p className="font-bold text-sm mt-2 mb-1">RIGHT BREAST:</p>
              <Prose text={summary.right_breast_findings} />
            </>
          )}
          {showLeft && (
            <>
              <p className="font-bold text-sm mt-3 mb-1">LEFT BREAST:</p>
              <Prose text={summary.left_breast_findings} />
            </>
          )}

          <Head>Opinion:</Head>
          <Prose text={summary.opinion} />
          <div className="mt-1 text-sm">
            {showRight && summary.birads_right != null && (
              <p>BI-RADS category {summary.birads_right} for right breast.</p>
            )}
            {showLeft && summary.birads_left != null && (
              <p>BI-RADS category {summary.birads_left} for left breast.</p>
            )}
          </div>
        </>
      )}
    </ReportShell>
  );
}
