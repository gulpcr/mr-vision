"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { AlertTriangle, Trash2, CheckCircle2, RefreshCw } from "lucide-react";

type ResetState = "idle" | "confirming" | "running" | "done" | "error";

export default function AdminToolsPage() {
  const [resetState, setResetState] = useState<ResetState>("idle");
  const [confirmText, setConfirmText] = useState("");
  const [result, setResult] = useState<Record<string, number> | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const CONFIRM_PHRASE = "RESET ALL DATA";

  const handleReset = async () => {
    if (confirmText !== CONFIRM_PHRASE) return;
    setResetState("running");
    setErrorMsg(null);
    try {
      const data = await api.admin.resetAllData();
      setResult(data.cleared);
      setResetState("done");
    } catch (e: any) {
      setErrorMsg(e.message || "Reset failed");
      setResetState("error");
    }
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Admin Tools</h1>
        <p className="text-sm text-gray-500 mt-1">Maintenance operations for platform administrators.</p>
      </div>

      {/* Danger zone card */}
      <div className="border-2 border-red-200 rounded-xl overflow-hidden">
        <div className="bg-red-50 px-5 py-4 flex items-center gap-3 border-b border-red-200">
          <AlertTriangle className="w-5 h-5 text-red-600 shrink-0" />
          <div>
            <p className="font-semibold text-red-900">Danger Zone</p>
            <p className="text-xs text-red-700">These operations are irreversible.</p>
          </div>
        </div>

        <div className="bg-white px-5 py-5 space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-gray-900">Reset All Clinical Data</h2>
            <p className="text-xs text-gray-500 mt-1 leading-relaxed">
              Permanently deletes all studies, series, AI jobs, results, critical alerts, review
              queue items, audit log, and all MinIO artifacts. Resets the platform to a clean
              state ready for a fresh dataset.
            </p>
            <p className="text-xs font-medium text-gray-600 mt-2">
              <span className="font-bold">Preserved:</span> user accounts, routing rules, alert
              rules, retention policies, model versions, use case registry.
            </p>
          </div>

          {/* Orthanc PACS note */}
          <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2.5 text-xs text-amber-800">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            DICOM data in Orthanc PACS is <strong className="mx-0.5">not</strong> affected — only
            the platform database and MinIO artifact storage are cleared. Re-ingest studies from
            Orthanc after reset to start fresh AI analysis.
          </div>

          {/* State: idle → show button */}
          {resetState === "idle" && (
            <button
              onClick={() => setResetState("confirming")}
              className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors"
            >
              <Trash2 className="w-4 h-4" />
              Reset All Data…
            </button>
          )}

          {/* State: confirming → show confirmation input */}
          {resetState === "confirming" && (
            <div className="space-y-3">
              <p className="text-sm text-gray-700">
                Type <span className="font-mono font-bold text-red-700">{CONFIRM_PHRASE}</span> to
                confirm:
              </p>
              <input
                autoFocus
                type="text"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder={CONFIRM_PHRASE}
                className="w-full px-3 py-2 text-sm border-2 border-red-300 rounded-lg focus:outline-none focus:border-red-500 font-mono"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && confirmText === CONFIRM_PHRASE) handleReset();
                  if (e.key === "Escape") {
                    setResetState("idle");
                    setConfirmText("");
                  }
                }}
              />
              <div className="flex gap-2">
                <button
                  onClick={handleReset}
                  disabled={confirmText !== CONFIRM_PHRASE}
                  className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg transition-colors"
                >
                  <Trash2 className="w-4 h-4" />
                  Confirm Reset
                </button>
                <button
                  onClick={() => { setResetState("idle"); setConfirmText(""); }}
                  className="px-4 py-2 text-sm font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* State: running */}
          {resetState === "running" && (
            <div className="flex items-center gap-3 text-sm text-gray-600">
              <RefreshCw className="w-4 h-4 animate-spin text-red-500" />
              Deleting records and clearing artifacts…
            </div>
          )}

          {/* State: done */}
          {resetState === "done" && result && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-green-700">
                <CheckCircle2 className="w-5 h-5" />
                <span className="font-semibold text-sm">Reset complete.</span>
              </div>
              <div className="bg-gray-50 border border-gray-200 rounded-lg px-4 py-3">
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                  Cleared
                </p>
                <dl className="grid grid-cols-2 gap-x-8 gap-y-1 text-sm">
                  {Object.entries(result).map(([key, val]) => (
                    <div key={key} className="flex justify-between">
                      <dt className="text-gray-500">{key.replace(/_/g, " ")}</dt>
                      <dd className="font-semibold text-gray-900 tabular-nums">{val}</dd>
                    </div>
                  ))}
                </dl>
              </div>
              <button
                onClick={() => { setResetState("idle"); setResult(null); setConfirmText(""); }}
                className="text-sm text-gray-500 hover:text-gray-700 underline"
              >
                Reset again
              </button>
            </div>
          )}

          {/* State: error */}
          {resetState === "error" && (
            <div className="space-y-2">
              <div className="flex items-start gap-2 text-red-700 text-sm">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                <span>{errorMsg}</span>
              </div>
              <button
                onClick={() => { setResetState("idle"); setConfirmText(""); }}
                className="text-sm text-gray-500 hover:text-gray-700 underline"
              >
                Try again
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
