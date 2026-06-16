"use client";

import { useState, useMemo } from "react";
import { useOrthancStudies, useStudies } from "@/lib/hooks";
import { api } from "@/lib/api";
import { formatDate, formatPatientName } from "@/lib/format";
import {
  Upload,
  RefreshCw,
  Search,
  CheckCircle,
  AlertCircle,
  ArrowRight,
  Network,
  Database,
  Brain,
  ChevronDown,
  ChevronUp,
  Info,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

// ── Modality badge colour ─────────────────────────────────────────────────────

function ModalityBadge({ modality }: { modality: string }) {
  const m = (modality || "").toUpperCase();
  const cfg: Record<string, string> = {
    MR:  "bg-blue-50 text-blue-700 border-blue-100",
    CT:  "bg-slate-100 text-slate-700 border-slate-200",
    PT:  "bg-amber-50 text-amber-700 border-amber-100",
    US:  "bg-teal-50 text-teal-700 border-teal-100",
    DX:  "bg-gray-100 text-gray-600 border-gray-200",
    CR:  "bg-gray-100 text-gray-600 border-gray-200",
    NM:  "bg-green-50 text-green-700 border-green-100",
    XA:  "bg-orange-50 text-orange-700 border-orange-100",
  };
  return (
    <span className={`px-2 py-0.5 rounded border text-xs font-semibold ${cfg[m] ?? "bg-gray-50 text-gray-600 border-gray-200"}`}>
      {m || "—"}
    </span>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function UploadPage() {
  const { data: orthancStudies, isLoading, mutate } = useOrthancStudies();
  const { data: platformData } = useStudies();
  const router = useRouter();

  const [search, setSearch] = useState("");
  const [ingesting, setIngesting] = useState<string | null>(null);
  const [ingestedUids, setIngestedUids] = useState<string[]>([]);
  const [ingestError, setIngestError] = useState<{ uid: string; msg: string } | null>(null);

  const [manualUid, setManualUid] = useState("");
  const [manualLoading, setManualLoading] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);
  const [showManual, setShowManual] = useState(false);

  // Build a set of UIDs already in the platform for quick lookup
  const platformUidSet = useMemo(
    () => new Set((platformData?.studies ?? []).map((s) => s.study_instance_uid)),
    [platformData]
  );

  const filtered = (orthancStudies ?? []).filter((s) => {
    const q = search.toLowerCase();
    return (
      !q ||
      s.patient_name?.toLowerCase().includes(q) ||
      s.patient_id?.toLowerCase().includes(q) ||
      s.study_description?.toLowerCase().includes(q) ||
      s.modality?.toLowerCase().includes(q)
    );
  });

  async function handleIngest(uid: string) {
    setIngesting(uid);
    setIngestError(null);
    try {
      await api.studies.ingest(uid);
      setIngestedUids((prev) => [...prev, uid]);
    } catch (e: any) {
      setIngestError({ uid, msg: e.message || "Ingest failed" });
    } finally {
      setIngesting(null);
    }
  }

  async function handleManualIngest(e: React.FormEvent) {
    e.preventDefault();
    const uid = manualUid.trim();
    if (!uid) return;
    const uidPattern = /^[0-9][0-9.]{0,62}[0-9]$/;
    if (!uidPattern.test(uid)) {
      setManualError("Invalid Study Instance UID — must contain only digits and dots, starting and ending with a digit. Example: 1.2.840.113619.2.55.3");
      return;
    }
    setManualLoading(true);
    setManualError(null);
    try {
      const study = await api.studies.ingest(uid);
      router.push(`/study/${study.study_instance_uid}`);
    } catch (err: any) {
      setManualError(err.message || "Failed to ingest study");
    } finally {
      setManualLoading(false);
    }
  }

  const alreadyInPlatform = (uid: string) =>
    platformUidSet.has(uid) || ingestedUids.includes(uid);

  return (
    <div className="space-y-6 max-w-6xl">

      {/* ── Page header ──────────────────────────────────────────────────── */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Upload / Ingest DICOM</h1>
        <p className="text-sm text-gray-500 mt-1">
          Bring studies from your PACS into the AI platform for analysis.
        </p>
      </div>

      {/* ── How it works ─────────────────────────────────────────────────── */}
      <div className="bg-blue-50 border border-blue-100 rounded-xl px-5 py-4">
        <p className="text-xs font-semibold text-blue-700 uppercase tracking-wider mb-3 flex items-center gap-1.5">
          <Info className="w-3.5 h-3.5" /> How it works
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="flex gap-3">
            <div className="shrink-0 w-7 h-7 rounded-full bg-white border border-blue-200 flex items-center justify-center text-xs font-bold text-blue-600">1</div>
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <Network className="w-5 h-5 text-blue-500" />
                <p className="text-sm font-semibold text-gray-800">Scanner → Orthanc PACS</p>
              </div>
              <p className="text-xs text-gray-600 leading-relaxed">DICOM files arrive from the scanner over the DICOM network and are stored in Orthanc (your on-site PACS). Nothing to do here — this happens automatically.</p>
            </div>
          </div>
          <div className="flex gap-3">
            <div className="shrink-0 w-7 h-7 rounded-full bg-white border border-blue-200 flex items-center justify-center text-xs font-bold text-blue-600">2</div>
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <Database className="w-5 h-5 text-primary-500" />
                <p className="text-sm font-semibold text-gray-800">Ingest into Platform</p>
              </div>
              <p className="text-xs text-gray-600 leading-relaxed">Click <strong>Ingest</strong> next to a study below. The platform reads the DICOM metadata from Orthanc and registers the study so AI can analyse it.</p>
            </div>
          </div>
          <div className="flex gap-3">
            <div className="shrink-0 w-7 h-7 rounded-full bg-white border border-blue-200 flex items-center justify-center text-xs font-bold text-blue-600">3</div>
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <Brain className="w-5 h-5 text-green-500" />
                <p className="text-sm font-semibold text-gray-800">Run AI Pipeline</p>
              </div>
              <p className="text-xs text-gray-600 leading-relaxed">Open the study, click <strong>Run AI</strong>, and select a pipeline (e.g. Brain MRI, PET/CT). Results appear on the study page when processing is complete.</p>
            </div>
          </div>
        </div>
      </div>

      {/* ── DICOM send config ─────────────────────────────────────────────── */}
      <div className="bg-white border border-gray-200 rounded-xl px-5 py-4">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">
          Configure your scanner to send DICOMs here
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          {[
            { label: "AE Title",          value: "ORTHANC" },
            { label: "Host / IP",         value: "localhost" },
            { label: "DICOM Port",        value: "4242" },
            { label: "HTTP (Orthanc UI)", value: "8042" },
          ].map(({ label, value }) => (
            <div key={label}>
              <p className="text-xs text-gray-400 mb-0.5">{label}</p>
              <p className="font-mono font-semibold text-gray-900 bg-gray-50 border border-gray-200 rounded px-2 py-1 text-xs">
                {value}
              </p>
            </div>
          ))}
        </div>
        <p className="text-xs text-gray-400 mt-3">
          After the scanner sends a study and Orthanc marks it "stable", it appears in the table below automatically. Hit <strong>Refresh</strong> if you don't see it yet.
        </p>
      </div>

      {/* ── Orthanc PACS Browser ──────────────────────────────────────────── */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <div>
            <h2 className="font-semibold text-gray-900">
              Studies in Orthanc PACS
              {orthancStudies && (
                <span className="ml-2 text-xs font-normal text-gray-400">
                  {orthancStudies.length} available
                </span>
              )}
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Studies with a <span className="text-green-600 font-medium">green tick</span> are already in the platform and ready for AI analysis.
            </p>
          </div>
          <button
            onClick={() => mutate()}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 hover:text-gray-900 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Refresh
          </button>
        </div>

        {/* Search */}
        <div className="px-5 py-3 border-b border-gray-100">
          <div className="relative">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter by patient name, MRN, description or modality…"
              className="w-full pl-9 pr-4 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
        </div>

        {/* Table */}
        {isLoading ? (
          <div className="p-10 text-center text-sm text-gray-400">Loading from Orthanc…</div>
        ) : !filtered.length ? (
          <div className="p-10 text-center space-y-2">
            <p className="text-sm text-gray-500 font-medium">
              {search ? "No studies match your filter" : "No studies in Orthanc yet"}
            </p>
            {!search && (
              <p className="text-xs text-gray-400">
                Send DICOMs from your scanner using the connection details above, or ask your PACS administrator.
              </p>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50/50">
                  <th className="text-left px-5 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider">Patient</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider">MRN</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider">Date</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider">Description</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider">Modality</th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider">Series</th>
                  <th className="px-5 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wider text-right">Status / Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {filtered.map((s) => {
                  const inPlatform = alreadyInPlatform(s.study_instance_uid);
                  const isIngesting = ingesting === s.study_instance_uid;
                  const hasError = ingestError?.uid === s.study_instance_uid;

                  return (
                    <tr
                      key={s.orthanc_id}
                      className={`transition-colors ${inPlatform ? "bg-green-50/30" : "hover:bg-gray-50"}`}
                    >
                      <td className="px-5 py-3">
                        <p className="font-medium text-gray-900">{formatPatientName(s.patient_name)}</p>
                      </td>
                      <td className="px-3 py-3 text-gray-500 font-mono text-xs">{s.patient_id || "—"}</td>
                      <td className="px-3 py-3 text-gray-600 whitespace-nowrap">{formatDate(s.study_date)}</td>
                      <td className="px-3 py-3 text-gray-600 max-w-[200px]">
                        <span className="truncate block" title={s.study_description || ""}>
                          {s.study_description || <span className="text-gray-400 italic">No description</span>}
                        </span>
                      </td>
                      <td className="px-3 py-3">
                        <ModalityBadge modality={s.modality} />
                      </td>
                      <td className="px-3 py-3 text-gray-600 text-center">{s.series_count}</td>
                      <td className="px-5 py-3 text-right">
                        {inPlatform ? (
                          <div className="flex items-center justify-end gap-2">
                            <span className="flex items-center gap-1 text-xs text-green-600 font-medium">
                              <CheckCircle className="w-3.5 h-3.5" />
                              In Platform
                            </span>
                            <Link
                              href={`/study/${s.study_instance_uid}`}
                              className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-primary-600 border border-primary-200 rounded-lg hover:bg-primary-50 transition-colors whitespace-nowrap"
                            >
                              Open <ArrowRight className="w-3 h-3" />
                            </Link>
                          </div>
                        ) : hasError ? (
                          <div className="flex items-center justify-end gap-1.5">
                            <span className="flex items-center gap-1 text-xs text-red-600">
                              <AlertCircle className="w-3.5 h-3.5" />
                              {ingestError!.msg}
                            </span>
                            <button
                              onClick={() => handleIngest(s.study_instance_uid)}
                              className="text-xs text-gray-500 hover:text-gray-700 underline"
                            >
                              Retry
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => handleIngest(s.study_instance_uid)}
                            disabled={isIngesting}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors ml-auto"
                          >
                            {isIngesting ? (
                              <><RefreshCw className="w-3 h-3 animate-spin" /> Ingesting…</>
                            ) : (
                              <><Upload className="w-3 h-3" /> Ingest</>
                            )}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Manual UID (collapsed by default) ────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200">
        <button
          onClick={() => setShowManual(!showManual)}
          className="w-full flex items-center justify-between px-5 py-4 text-left hover:bg-gray-50 transition-colors rounded-xl"
        >
          <div>
            <p className="text-sm font-semibold text-gray-700">Ingest by Study Instance UID</p>
            <p className="text-xs text-gray-400 mt-0.5">
              Use this if the study doesn't appear above (e.g. sent via a worklist or external PACS).
            </p>
          </div>
          {showManual ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
        </button>

        {showManual && (
          <div className="px-5 pb-5 border-t border-gray-100 pt-4">
            <p className="text-xs text-gray-500 mb-3">
              The <strong>Study Instance UID</strong> is a DICOM tag (0020,000D) that uniquely identifies the study. You can find it in the Orthanc web UI at{" "}
              <code className="text-xs bg-gray-100 px-1 rounded">http://localhost:8042</code> or in the scanner's DICOM worklist.
            </p>
            <form onSubmit={handleManualIngest} className="flex gap-3">
              <input
                type="text"
                value={manualUid}
                onChange={(e) => { setManualUid(e.target.value); setManualError(null); }}
                placeholder="1.2.840.113619.2.55.3…"
                className="flex-1 px-4 py-2 text-sm font-mono border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
              <button
                type="submit"
                disabled={manualLoading || !manualUid.trim()}
                className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors whitespace-nowrap"
              >
                <Upload className="w-4 h-4" />
                {manualLoading ? "Ingesting…" : "Ingest & Open"}
              </button>
            </form>
            {manualError && (
              <p className="mt-2 text-sm text-red-600 flex items-start gap-1.5">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                {manualError}
              </p>
            )}
          </div>
        )}
      </div>

    </div>
  );
}
