"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useStudy, useStudyResults, useShareLinks } from "@/lib/hooks";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import {
  ArrowLeft, Share2, Copy, Trash2, CheckCircle2, XCircle, Clock, Truck,
} from "lucide-react";

// Channels this platform can theoretically deliver a report through. Only
// "portal" is actually implemented today (verified against the backend) —
// the rest are listed so the screen is honest about what's coming, not
// fabricated as working controls. Do not add a control for a channel here
// until it is genuinely wired up end-to-end in the backend.
const OTHER_CHANNELS = [
  { label: "DICOM Structured Report" },
  { label: "FHIR Resource" },
  { label: "RIS / HIS Integration" },
  { label: "WhatsApp" },
];

function shareStatus(expiresAt: string | null, isActive: boolean): "active" | "expired" | "revoked" {
  if (!isActive) return "revoked";
  if (expiresAt && new Date(expiresAt).getTime() < Date.now()) return "expired";
  return "active";
}

function ResultDeliveryCard({
  resultId,
  usecaseName,
}: {
  resultId: string;
  usecaseName: string;
}) {
  const { data: shares, isLoading, mutate } = useShareLinks(resultId);
  const { user } = useAuth();
  const [showCreate, setShowCreate] = useState(false);
  const [ttlDays, setTtlDays] = useState(7);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [newLinkUrl, setNewLinkUrl] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [pendingRevokeId, setPendingRevokeId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleCreate = async () => {
    setCreateBusy(true);
    setCreateError(null);
    try {
      const link = await api.portal.createShareLink(resultId, user?.username || "unknown", ttlDays);
      setNewLinkUrl(`${window.location.origin}/portal/${link.token}`);
      mutate();
    } catch (e: any) {
      setCreateError(e.message || "Failed to create share link");
    } finally {
      setCreateBusy(false);
    }
  };

  const closeCreate = () => {
    setShowCreate(false);
    setNewLinkUrl(null);
    setCreateError(null);
  };

  const copyExisting = (id: string, token: string) => {
    // token is truncated in the list response (security) — the full link is
    // only ever known at creation time, so "copy" here just re-shares what we
    // have; for a fresh copyable link, create a new one.
    navigator.clipboard.writeText(`${window.location.origin}/portal/${token}`);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100">
          {usecaseName.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
        </h2>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Share2 className="w-3.5 h-3.5" /> New Portal Link
        </button>
      </div>

      {actionError && <ErrorBanner message={actionError} onDismiss={() => setActionError(null)} className="mb-3" />}

      {isLoading ? (
        <p className="text-sm text-gray-400 dark:text-gray-500">Loading delivery status…</p>
      ) : !shares || shares.length === 0 ? (
        <p className="text-sm text-gray-400 dark:text-gray-500 italic">No portal links created yet for this report.</p>
      ) : (
        <div className="space-y-2">
          {shares.map((s) => {
            const status = shareStatus(s.expires_at, s.is_active);
            return (
              <div key={s.id} className="flex items-center justify-between gap-3 text-sm border border-gray-100 dark:border-gray-800 rounded-lg px-3 py-2">
                <div className="flex items-center gap-2 min-w-0">
                  {status === "active" ? (
                    <CheckCircle2 className="w-4 h-4 text-green-500 dark:text-green-400 shrink-0" />
                  ) : (
                    <XCircle className="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" />
                  )}
                  <span className="font-mono text-xs text-gray-500 dark:text-gray-400 truncate">{s.token}</span>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full shrink-0 ${
                    status === "active"
                      ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300"
                      : status === "expired"
                      ? "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300"
                      : "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
                  }`}>
                    {status === "active" ? "Active" : status === "expired" ? "Expired" : "Revoked"}
                  </span>
                </div>
                <div className="flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500 shrink-0">
                  <span>Created by {s.created_by}</span>
                  {s.expires_at && (
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" /> Expires {formatDateTime(s.expires_at)}
                    </span>
                  )}
                  {status === "active" && (
                    <button
                      onClick={() => setPendingRevokeId(s.id)}
                      aria-label="Revoke this share link"
                      className="p-1 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Modal open={showCreate} onClose={closeCreate} title="Create Portal Share Link" size="sm">
        {newLinkUrl ? (
          <div className="space-y-3">
            <p className="text-sm text-gray-700 dark:text-gray-300">Share link created:</p>
            <div className="flex items-center gap-2">
              <input readOnly value={newLinkUrl} className="flex-1 text-xs font-mono border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5" />
              <button
                onClick={() => { navigator.clipboard.writeText(newLinkUrl); }}
                className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 border border-gray-200 dark:border-gray-700 rounded-lg"
                aria-label="Copy link"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
            <button onClick={closeCreate} className="w-full px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 rounded-lg transition-colors">
              Done
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {createError && <ErrorBanner message={createError} />}
            <label className="block text-xs">
              <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Expires In (days)</span>
              <input
                type="number"
                min={1}
                max={90}
                value={ttlDays}
                onChange={(e) => setTtlDays(Number(e.target.value))}
                className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
              />
            </label>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={closeCreate} className="px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-400 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg transition-colors">
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={createBusy}
                className="px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 rounded-lg transition-colors"
              >
                {createBusy ? "Creating…" : "Create Link"}
              </button>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingRevokeId !== null}
        title="Revoke Share Link"
        consequence="This link will stop working immediately for anyone who has it. This cannot be undone."
        confirmLabel="Revoke"
        onConfirm={async () => {
          if (!pendingRevokeId) return;
          try {
            await api.portal.revokeShare(pendingRevokeId);
            mutate();
          } catch (e: any) {
            setActionError(e.message || "Failed to revoke link");
          }
          setPendingRevokeId(null);
        }}
        onCancel={() => setPendingRevokeId(null)}
      />
    </div>
  );
}

export default function DeliveryStatusPage() {
  const params = useParams();
  const uid = params.uid as string;
  const { data: study, isLoading: studyLoading } = useStudy(uid);
  const { data: results, isLoading: resultsLoading } = useStudyResults(uid);

  const loading = studyLoading || resultsLoading;

  return (
    <div className="max-w-3xl mx-auto p-4 space-y-4">
      <div className="no-print mb-2">
        <Link
          href={`/study/${uid}`}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
        >
          <ArrowLeft className="w-4 h-4" /> Back to study
        </Link>
      </div>

      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Delivery Status</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
          {study?.patient_name || "—"} · Report delivery state per channel
        </p>
      </div>

      {loading ? (
        <p className="text-sm text-gray-400 dark:text-gray-500">Loading…</p>
      ) : !results || results.length === 0 ? (
        <EmptyState icon={Truck} title="No results to deliver yet" description="Run an AI pipeline on this study first." />
      ) : (
        results.map((r) => <ResultDeliveryCard key={r.id} resultId={r.id} usecaseName={r.usecase_name} />)
      )}

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-1">Other Delivery Channels</h2>
        <p className="text-xs text-gray-400 dark:text-gray-500 mb-3">
          Not yet implemented in this deployment — shown for visibility, not configurable here.
        </p>
        <div className="space-y-2">
          {OTHER_CHANNELS.map((c) => (
            <div key={c.label} className="flex items-center justify-between text-sm px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-800/50">
              <span className="text-gray-500 dark:text-gray-400">{c.label}</span>
              <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500">
                Not available
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
