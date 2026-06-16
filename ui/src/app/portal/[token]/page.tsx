"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, Result } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { FileDown, AlertTriangle, CheckCircle, Activity, Clock, Building2, User, Hash } from "lucide-react";

interface PortalData {
  portal: boolean;
  expires_at: string;
  result: Result;
  study: {
    patient_name: string | null;
    patient_id: string | null;
    study_date: string | null;
    study_description: string | null;
    institution_name: string | null;
  };
}

function flattenMeasurements(obj: Record<string, any>, prefix = ""): Array<{ key: string; value: any }> {
  const out: Array<{ key: string; value: any }> = [];
  for (const [k, v] of Object.entries(obj)) {
    const label = prefix ? `${prefix} / ${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      out.push(...flattenMeasurements(v, label));
    } else {
      out.push({ key: label, value: v });
    }
  }
  return out;
}

function formatKey(key: string): string {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatValue(value: any): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "number") return value % 1 === 0 ? String(value) : value.toFixed(2);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

export default function PortalPage() {
  const params = useParams();
  const token = params.token as string;

  const [data, setData] = useState<PortalData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pdfLoading, setPdfLoading] = useState(false);

  useEffect(() => {
    api.portal
      .getByToken(token)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [token]);

  const handleDownloadPdf = async () => {
    if (!data || pdfLoading) return;
    setPdfLoading(true);
    try {
      const blob = await api.pdf.downloadBlob(data.result.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `report_${data.result.id.slice(0, 8)}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: any) {
      alert("PDF download failed: " + e.message);
    } finally {
      setPdfLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <Activity className="w-10 h-10 text-blue-500 mx-auto mb-3 animate-pulse" />
          <p className="text-gray-500">Loading report...</p>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-8 max-w-md w-full mx-4">
          <AlertTriangle className="w-10 h-10 text-red-400 mx-auto mb-3" />
          <h1 className="text-lg font-semibold text-gray-900 text-center mb-2">Access Denied</h1>
          <p className="text-gray-500 text-sm text-center">
            {error || "This link is invalid or has expired. Please request a new link from the radiology department."}
          </p>
        </div>
      </div>
    );
  }

  const { result, study } = data;
  const measurements = flattenMeasurements(result.measurements);
  const summaryEntries = flattenMeasurements(result.summary);
  const expiresAt = new Date(data.expires_at);
  const now = new Date();
  const daysLeft = Math.ceil((expiresAt.getTime() - now.getTime()) / (1000 * 60 * 60 * 24));

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Portal Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Activity className="w-7 h-7 text-blue-600" />
            <div>
              <h1 className="text-base font-bold text-gray-900">MRI AI Platform</h1>
              <p className="text-xs text-gray-500">Referring Physician Portal — Read Only</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              <Clock className="w-3.5 h-3.5" />
              Expires {daysLeft > 0 ? `in ${daysLeft} day${daysLeft !== 1 ? "s" : ""}` : "today"}
            </div>
            <button
              onClick={handleDownloadPdf}
              disabled={pdfLoading}
              className="flex items-center gap-2 px-3 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              <FileDown className="w-4 h-4" />
              {pdfLoading ? "Generating..." : "Download PDF"}
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8 space-y-6">
        {/* Patient / Study Info */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
          <div className="bg-gradient-to-r from-blue-600 to-blue-700 px-6 py-4">
            <h2 className="text-lg font-bold text-white">
              {study.patient_name || "Unknown Patient"}
            </h2>
            <p className="text-blue-100 text-sm mt-0.5">
              {study.study_description || result.usecase_name.replace(/_/g, " ")}
            </p>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-0 divide-x divide-y divide-gray-100">
            <div className="px-4 py-3">
              <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
                <User className="w-3 h-3" /> Patient ID
              </div>
              <p className="text-sm font-semibold text-gray-900">{study.patient_id || "-"}</p>
            </div>
            <div className="px-4 py-3">
              <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
                <Hash className="w-3 h-3" /> Study Date
              </div>
              <p className="text-sm font-semibold text-gray-900">{formatDate(study.study_date)}</p>
            </div>
            <div className="px-4 py-3">
              <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
                <Building2 className="w-3 h-3" /> Institution
              </div>
              <p className="text-sm font-semibold text-gray-900">{study.institution_name || "-"}</p>
            </div>
            <div className="px-4 py-3">
              <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
                <Activity className="w-3 h-3" /> AI Model
              </div>
              <p className="text-sm font-semibold text-gray-900">{result.model_version}</p>
            </div>
          </div>
        </div>

        {/* QA Flags */}
        {result.qa_flags.length > 0 && (
          <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
            <div className="flex items-center gap-2 mb-2">
              <AlertTriangle className="w-4 h-4 text-amber-600" />
              <h3 className="text-sm font-semibold text-amber-800">Quality Assurance Flags</h3>
            </div>
            <div className="flex flex-wrap gap-2">
              {result.qa_flags.map((flag) => (
                <span key={flag} className="px-2 py-1 bg-amber-100 text-amber-800 text-xs rounded-lg font-medium">
                  {flag}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Summary */}
        {summaryEntries.length > 0 && (
          <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h3 className="text-sm font-semibold text-gray-900">AI Summary</h3>
            </div>
            <div className="px-6 py-4 grid grid-cols-1 md:grid-cols-2 gap-3">
              {summaryEntries.map(({ key, value }) => (
                <div key={key} className="flex justify-between items-center py-1.5 border-b border-gray-50">
                  <span className="text-sm text-gray-600">{formatKey(key)}</span>
                  <span className="text-sm font-semibold text-gray-900">{formatValue(value)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Measurements */}
        {measurements.length > 0 && (
          <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h3 className="text-sm font-semibold text-gray-900">Quantitative Measurements</h3>
              <p className="text-xs text-gray-400 mt-0.5">
                AI-generated measurements from the {result.usecase_name.replace(/_/g, " ")} model
              </p>
            </div>
            <table className="w-full">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  <th className="text-left px-6 py-2.5 text-xs font-semibold text-gray-500 uppercase">Parameter</th>
                  <th className="text-right px-6 py-2.5 text-xs font-semibold text-gray-500 uppercase">Value</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {measurements.map(({ key, value }) => (
                  <tr key={key} className="hover:bg-gray-50">
                    <td className="px-6 py-3 text-sm text-gray-700">{formatKey(key)}</td>
                    <td className="px-6 py-3 text-sm font-semibold text-gray-900 text-right">{formatValue(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* No QA flags = passed */}
        {result.qa_flags.length === 0 && (
          <div className="bg-green-50 border border-green-200 rounded-xl p-4 flex items-center gap-3">
            <CheckCircle className="w-5 h-5 text-green-600 shrink-0" />
            <div>
              <p className="text-sm font-semibold text-green-800">Quality Check Passed</p>
              <p className="text-xs text-green-600 mt-0.5">No quality flags were raised for this analysis.</p>
            </div>
          </div>
        )}

        {/* Disclaimer */}
        <div className="bg-gray-100 rounded-xl p-4">
          <p className="text-xs text-gray-500 text-center">
            This report is generated by an AI-assisted analysis system and is intended for informational purposes only.
            All clinical decisions must be made by a qualified radiologist based on direct image review.
            Report generated: {formatDate(result.created_at)} &middot; Model: {result.model_version}
          </p>
        </div>
      </main>
    </div>
  );
}
