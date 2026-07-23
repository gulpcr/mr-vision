"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { AlertTriangle, Trash2, CheckCircle2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";

const CONFIRM_PHRASE = "RESET ALL DATA";

export default function AdminToolsPage() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [result, setResult] = useState<Record<string, number> | null>(null);

  const handleReset = async () => {
    const data = await api.admin.resetAllData();
    setResult(data.cleared);
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Admin Tools</h1>
        <p className="text-sm text-gray-500 mt-1">Maintenance operations for platform administrators.</p>
      </div>

      {/* Danger zone card */}
      <div className="border-2 border-red-200 dark:border-red-900 rounded-xl overflow-hidden">
        <div className="bg-red-50 dark:bg-red-950 px-5 py-4 flex items-center gap-3 border-b border-red-200 dark:border-red-900">
          <AlertTriangle className="w-5 h-5 text-red-600 shrink-0" />
          <div>
            <p className="font-semibold text-red-900 dark:text-red-200">Danger Zone</p>
            <p className="text-xs text-red-700 dark:text-red-400">These operations are irreversible.</p>
          </div>
        </div>

        <div className="bg-surface px-5 py-5 space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Reset All Clinical Data</h2>
            <p className="text-xs text-gray-500 mt-1 leading-relaxed">
              Permanently deletes all studies, series, AI jobs, results, critical alerts, review
              queue items, audit log, and all MinIO artifacts. Resets the platform to a clean
              state ready for a fresh dataset.
            </p>
            <p className="text-xs font-medium text-gray-600 dark:text-gray-400 mt-2">
              <span className="font-bold">Preserved:</span> user accounts, routing rules, alert
              rules, retention policies, model versions, use case registry.
            </p>
          </div>

          {/* Orthanc PACS note */}
          <div className="flex items-start gap-2 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-900 rounded-lg px-3 py-2.5 text-xs text-amber-800 dark:text-amber-300">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            DICOM data in Orthanc PACS is <strong className="mx-0.5">not</strong> affected — only
            the platform database and MinIO artifact storage are cleared. Re-ingest studies from
            Orthanc after reset to start fresh AI analysis.
          </div>

          <button
            onClick={() => setDialogOpen(true)}
            className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors"
          >
            <Trash2 className="w-4 h-4" />
            Reset All Data…
          </button>

          {result && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-green-700 dark:text-green-400">
                <CheckCircle2 className="w-5 h-5" />
                <span className="font-semibold text-sm">Reset complete.</span>
              </div>
              <div className="bg-surface-raised border border-border rounded-lg px-4 py-3">
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                  Cleared
                </p>
                <dl className="grid grid-cols-2 gap-x-8 gap-y-1 text-sm">
                  {Object.entries(result).map(([key, val]) => (
                    <div key={key} className="flex justify-between">
                      <dt className="text-gray-500">{key.replace(/_/g, " ")}</dt>
                      <dd className="font-semibold text-gray-900 dark:text-gray-100 tabular-nums">{val}</dd>
                    </div>
                  ))}
                </dl>
              </div>
              <button
                onClick={() => setResult(null)}
                className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 underline"
              >
                Reset again
              </button>
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        tier="type-to-confirm"
        open={dialogOpen}
        title="Reset All Data"
        consequence="This permanently deletes all studies, series, AI jobs, results, critical alerts, review queue items, audit log entries, and MinIO artifacts. This cannot be undone."
        confirmPhrase={CONFIRM_PHRASE}
        confirmLabel="Confirm Reset"
        onConfirm={handleReset}
        onCancel={() => setDialogOpen(false)}
      />
    </div>
  );
}
