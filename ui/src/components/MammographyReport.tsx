"use client";

import { useEffect, useState } from "react";
import { api, Study, Result, MammographyReportData } from "@/lib/api";
import { Printer, Save, FileDown, CheckCircle2, AlertTriangle, X } from "lucide-react";

// Standalone formal bilateral mammography report (AECH-KIRAN layout). Editable
// by the radiologist; pre-filled from the mammography AI result, then saved as a
// report record and rendered to PDF server-side.

const HOSPITAL_NAME = "AECH-KIRAN";
const HOSPITAL_SUBTITLE =
  "Atomic Energy Cancer Hospital — Karachi Institute of Radiotherapy and Nuclear Medicine (KIRAN)";
const FOOTER_ADDRESS =
  "Haider Bux Gabol Road, Gulzar-e-Hijri, KDA Scheme 33, Karachi. Ph: 021-99261601-04 Ext. 222, 345";

type FormState = Record<string, string>;

const FIELDS = [
  "laterality", "file_no", "status", "contact", "procedure", "clinical_features",
  "right_breast_findings", "left_breast_findings", "opinion",
  "birads_right", "birads_left", "reviewing_doctor", "reporting_doctor",
];

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

export function MammographyReport({ study, result }: { study: Study; result: Result | null }) {
  const [form, setForm] = useState<FormState>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  // Defaults pre-filled from the AI result summary.
  useEffect(() => {
    let active = true;
    const summary: any = result?.summary || {};
    const lat = (summary.laterality as string) || "bilateral";
    const scope = lat === "right" ? "of the right breast " : lat === "left" ? "of the left breast " : "of both breasts ";
    const aiDefaults: FormState = {
      laterality: lat,
      procedure: `Digital mammography ${scope}performed in routine CC and MLO views.`,
      clinical_features: "",
      right_breast_findings: summary.right_breast_findings || "",
      left_breast_findings: summary.left_breast_findings || "",
      opinion: summary.opinion || "",
      birads_right: summary.birads_right != null ? String(summary.birads_right) : "",
      birads_left: summary.birads_left != null ? String(summary.birads_left) : "",
      file_no: "", status: "", contact: "", reviewing_doctor: "", reporting_doctor: "",
    };
    api.mammography
      .getReport(study.study_instance_uid)
      .then((saved: MammographyReportData) => {
        if (!active) return;
        // Saved (radiologist) values override AI defaults; only non-empty saved fields win.
        const merged: FormState = { ...aiDefaults };
        for (const k of FIELDS) {
          const v = (saved as any)?.[k];
          if (v != null && v !== "") merged[k] = String(v);
        }
        setForm(merged);
      })
      .catch(() => { if (active) setForm(aiDefaults); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [study.study_instance_uid, result]);

  const save = async () => {
    setSaving(true); setError(null); setSuccess(null);
    try {
      const payload: Partial<MammographyReportData> = {};
      for (const k of FIELDS) (payload as any)[k] = form[k] ?? "";
      await api.mammography.saveReport(study.study_instance_uid, payload);
      setSuccess("Report saved.");
    } catch (e: any) {
      setError(e.message || "Failed to save report");
    } finally {
      setSaving(false);
    }
  };

  const downloadPdf = async () => {
    setPdfLoading(true); setError(null);
    try {
      // Ensure the latest edits are persisted before the server renders the PDF.
      await save();
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

  if (loading) return <p className="text-center text-sm text-gray-400 py-12">Loading report…</p>;

  const laterality = (form.laterality || "bilateral").toLowerCase();
  const showRight = laterality !== "left";
  const showLeft = laterality !== "right";
  const title = laterality === "right" ? "RIGHT MAMMOGRAPHY"
    : laterality === "left" ? "LEFT MAMMOGRAPHY" : "BILATERAL MAMMOGRAPHY";

  const inputCls = "w-full px-2 py-1.5 text-sm border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-primary-500";
  const areaCls = `${inputCls} min-h-[80px] leading-relaxed`;
  const Head = ({ children }: { children: React.ReactNode }) => (
    <p className="font-bold text-sm mt-4 mb-1">{children}</p>
  );

  return (
    <div className="mx-auto max-w-3xl">
      {/* Toolbar (hidden in print) */}
      <div className="no-print flex items-center justify-end gap-2 mb-3">
        <button onClick={save} disabled={saving}
          className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50">
          <Save className="w-4 h-4" /> {saving ? "Saving…" : "Save"}
        </button>
        <button onClick={downloadPdf} disabled={pdfLoading}
          className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50">
          <FileDown className="w-4 h-4" /> {pdfLoading ? "Generating…" : "Download PDF"}
        </button>
        <button onClick={() => window.print()}
          className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50">
          <Printer className="w-4 h-4" /> Print
        </button>
      </div>

      {success && (
        <div className="no-print flex items-center gap-3 bg-green-50 border border-green-200 rounded-lg px-4 py-2.5 mb-3">
          <CheckCircle2 className="w-4 h-4 text-green-500 shrink-0" />
          <p className="text-sm text-green-800 flex-1">{success}</p>
          <button onClick={() => setSuccess(null)} className="text-green-400 hover:text-green-600"><X className="w-4 h-4" /></button>
        </div>
      )}
      {error && (
        <div className="no-print flex items-start gap-3 bg-red-50 border border-red-200 rounded-lg px-4 py-2.5 mb-3">
          <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
          <p className="text-sm text-red-700 flex-1">{error}</p>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600"><X className="w-4 h-4" /></button>
        </div>
      )}

      <div className="bg-white border border-gray-300 rounded-lg p-8 text-sm text-gray-900 print:border-0 print:p-0">
        {/* Hospital header */}
        <h1 className="text-center text-xl font-bold tracking-wide">{HOSPITAL_NAME}</h1>
        <p className="text-center text-xs text-gray-500 mb-4">{HOSPITAL_SUBTITLE}</p>

        {/* Patient table */}
        <div className="grid grid-cols-3 gap-x-6 gap-y-2 border border-gray-800 rounded p-3 mb-3">
          <div><span className="font-bold">PRN:</span> {study.patient_id || "—"}</div>
          <div className="flex items-center gap-1">
            <span className="font-bold whitespace-nowrap">File No.:</span>
            <input className={inputCls} value={form.file_no || ""} onChange={(e) => set("file_no", e.target.value)} placeholder="NIL" />
          </div>
          <div className="flex items-center gap-1">
            <span className="font-bold whitespace-nowrap">Status:</span>
            <input className={inputCls} value={form.status || ""} onChange={(e) => set("status", e.target.value)} />
          </div>
          <div><span className="font-bold">Name:</span> {study.patient_name || "—"}</div>
          <div><span className="font-bold">Age/Gender:</span> {fmtAge(study.patient_age)} / {fmtSex(study.patient_sex)}</div>
          <div className="flex items-center gap-1">
            <span className="font-bold whitespace-nowrap">Contact:</span>
            <input className={inputCls} value={form.contact || ""} onChange={(e) => set("contact", e.target.value)} />
          </div>
          <div><span className="font-bold">Entry Date:</span> {fmtDate(study.study_date)}</div>
        </div>

        <p className="text-center font-bold text-base my-3">{title}</p>

        <Head>Procedure:</Head>
        <textarea className={areaCls} value={form.procedure || ""} onChange={(e) => set("procedure", e.target.value)} />

        <Head>Clinical Features:</Head>
        <textarea className={areaCls} value={form.clinical_features || ""} onChange={(e) => set("clinical_features", e.target.value)} />

        <Head>Findings:</Head>
        {showRight && (
          <>
            <p className="font-bold italic text-sm mt-2 mb-1">RIGHT BREAST:</p>
            <textarea className={areaCls} value={form.right_breast_findings || ""} onChange={(e) => set("right_breast_findings", e.target.value)} />
          </>
        )}
        {showLeft && (
          <>
            <p className="font-bold italic text-sm mt-2 mb-1">LEFT BREAST:</p>
            <textarea className={areaCls} value={form.left_breast_findings || ""} onChange={(e) => set("left_breast_findings", e.target.value)} />
          </>
        )}

        <Head>Opinion:</Head>
        <textarea className={areaCls} value={form.opinion || ""} onChange={(e) => set("opinion", e.target.value)} />
        <div className="flex flex-wrap gap-4 mt-2">
          {showRight && (
          <label className="flex items-center gap-2 text-sm">
            <span className="font-bold">BI-RADS (Right):</span>
            <select className={inputCls + " w-auto"} value={form.birads_right || ""} onChange={(e) => set("birads_right", e.target.value)}>
              <option value="">—</option>
              {[0, 1, 2, 3, 4, 5, 6].map((n) => <option key={n} value={String(n)}>{n}</option>)}
            </select>
          </label>
          )}
          {showLeft && (
          <label className="flex items-center gap-2 text-sm">
            <span className="font-bold">BI-RADS (Left):</span>
            <select className={inputCls + " w-auto"} value={form.birads_left || ""} onChange={(e) => set("birads_left", e.target.value)}>
              <option value="">—</option>
              {[0, 1, 2, 3, 4, 5, 6].map((n) => <option key={n} value={String(n)}>{n}</option>)}
            </select>
          </label>
          )}
        </div>

        {/* Signatories */}
        <div className="grid grid-cols-2 gap-8 mt-12">
          <div className="text-center">
            <input className={inputCls + " text-center font-bold"} value={form.reviewing_doctor || ""} onChange={(e) => set("reviewing_doctor", e.target.value)} placeholder="Name" />
            <p className="text-xs text-gray-600 mt-1 font-medium">Reviewing Doctor</p>
          </div>
          <div className="text-center">
            <input className={inputCls + " text-center font-bold"} value={form.reporting_doctor || ""} onChange={(e) => set("reporting_doctor", e.target.value)} placeholder="Name" />
            <p className="text-xs text-gray-600 mt-1 font-medium">Reporting Doctor</p>
          </div>
        </div>

        <p className="text-center text-[10px] text-gray-400 mt-8 pt-3 border-t border-gray-100">{FOOTER_ADDRESS}</p>
      </div>
    </div>
  );
}
