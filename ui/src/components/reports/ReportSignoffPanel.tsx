"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type ReviewPriority, type SignoffState } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";
import { PriorityBadge } from "./PriorityBadge";
import { SignReportDialog } from "./SignReportDialog";
import { AlertTriangle, BadgeCheck, MessageSquare, PenLine, Send } from "lucide-react";

interface ReportSignoffPanelProps {
  studyUid: string;
  /** Called after a signature (or other change) so the host can reload the study. */
  onChanged?: () => void;
  /** Bump to force a reload (e.g. after the host signed through its own button). */
  refreshKey?: number;
  className?: string;
}

const roleLabel = (r: string | null) => (r ? r.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) : "");

// Priority (with manual override), the electronic signature — or the Sign button — and
// the report's comment thread. Shown beside the report on the study page and under
// every narrative report.
export function ReportSignoffPanel({ studyUid, onChanged, refreshKey = 0, className = "" }: ReportSignoffPanelProps) {
  const { can } = useAuth();
  const canSign = can("result.approve");
  const canComment = can("report.comment");
  const [state, setState] = useState<SignoffState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signOpen, setSignOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [posting, setPosting] = useState(false);

  const load = useCallback(() => {
    api.signoff.get(studyUid).then(setState).catch((e) => setError(e.message || "Failed to load sign-off"));
  }, [studyUid]);

  useEffect(() => { load(); }, [load, refreshKey]);

  if (!state) {
    return error ? <p className={`text-xs text-red-600 dark:text-red-400 ${className}`}>{error}</p> : null;
  }

  const signed = state.reading_status === "signed";
  const signature = state.signatures[0];

  const setPriority = async (value: string) => {
    setError(null);
    try {
      setState(await api.signoff.setPriority(studyUid, value === "auto" ? null : (value as ReviewPriority)));
    } catch (e: any) {
      setError(e.message || "Failed to change priority");
    }
  };

  const post = async () => {
    if (!comment.trim()) return;
    setPosting(true);
    setError(null);
    try {
      const c = await api.signoff.addComment(studyUid, comment.trim());
      setState({ ...state, comments: [...state.comments, c] });
      setComment("");
    } catch (e: any) {
      setError(e.message || "Failed to add comment");
    } finally {
      setPosting(false);
    }
  };

  return (
    <div className={`space-y-4 text-sm ${className}`}>
      {/* Priority */}
      <div className="no-print">
        <div className="flex flex-wrap items-center gap-2">
          <PriorityBadge priority={state.priority} overridden={state.priority_overridden} />
          {canSign && !signed && (
            <select
              aria-label="Override priority"
              value={state.priority_overridden ? state.priority : "auto"}
              onChange={(e) => setPriority(e.target.value)}
              className="text-xs border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1"
            >
              <option value="auto">Automatic ({state.computed_priority})</option>
              <option value="critical">Critical</option>
              <option value="abnormal">Abnormal</option>
              <option value="normal">Normal</option>
            </select>
          )}
        </div>
        {state.priority_reasons.length > 0 && (
          <ul className="mt-1.5 text-xs text-gray-500 dark:text-gray-400 list-disc pl-4">
            {state.priority_reasons.map((r) => <li key={r}>{r}</li>)}
          </ul>
        )}
        {state.priority_overridden && state.priority_override_by && (
          <p className="mt-1 text-[11px] text-gray-400">Set by {state.priority_override_by}</p>
        )}
      </div>

      {/* Signature */}
      {signed && signature ? (
        <div className="rounded-xl border border-emerald-300 dark:border-emerald-800 bg-emerald-50/60 dark:bg-emerald-950/40 p-3">
          <p className="flex items-center gap-1.5 font-semibold text-emerald-700 dark:text-emerald-300">
            <BadgeCheck className="w-4 h-4" /> Electronically signed
          </p>
          <p className="mt-1 text-gray-800 dark:text-gray-100">
            <span className="font-semibold">{signature.signer_full_name}</span>
            {signature.signer_role && <span className="text-gray-500 dark:text-gray-400"> ({roleLabel(signature.signer_role)})</span>}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400">{formatDateTime(signature.signed_at)}</p>
          <p className="mt-2 text-gray-700 dark:text-gray-200 whitespace-pre-wrap">
            <span className="font-medium">Comment: </span>{signature.comment}
          </p>
          <details className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            <summary className="cursor-pointer select-none">Attestation &amp; integrity</summary>
            <p className="mt-1 italic leading-relaxed">{signature.statement_text}</p>
            <p className="mt-1 font-mono break-all">SHA-256 {signature.content_hash}</p>
          </details>
          {state.integrity === "changed" && (
            <p className="mt-2 flex items-start gap-1.5 text-xs font-medium text-red-700 dark:text-red-400">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              The AI results or report changed after this signature — the signature does not cover the current content.
            </p>
          )}
        </div>
      ) : (
        <div className="no-print">
          {canSign ? (
            <>
              <button
                onClick={() => setSignOpen(true)}
                disabled={!state.has_results}
                title={state.has_results ? undefined : "No AI result to sign yet"}
                className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 text-sm font-semibold rounded-xl text-white bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40"
              >
                <PenLine className="w-4 h-4" /> Sign report
              </button>
              {state.assigned_to_username && (
                <p className="mt-1 text-[11px] text-center text-gray-400">Assigned to {state.assigned_to_username}</p>
              )}
            </>
          ) : (
            <p className="text-xs text-gray-500 dark:text-gray-400">Awaiting radiologist signature.</p>
          )}
        </div>
      )}

      {/* Comments */}
      {(state.comments.length > 0 || canComment) && (
        <div className="no-print">
          <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-2">
            <MessageSquare className="w-3.5 h-3.5" /> Comments ({state.comments.length})
          </p>
          <ul className="space-y-2 max-h-64 overflow-y-auto">
            {state.comments.map((c) => (
              <li key={c.id} className="rounded-lg bg-black/[0.03] dark:bg-white/[0.04] px-3 py-2">
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  <span className="font-semibold text-gray-700 dark:text-gray-200">{c.author_full_name || c.author_username}</span>
                  {c.author_role && ` · ${roleLabel(c.author_role)}`} · {formatDateTime(c.created_at)}
                </p>
                <p className="mt-0.5 text-gray-800 dark:text-gray-100 whitespace-pre-wrap">{c.body}</p>
              </li>
            ))}
          </ul>
          {canComment && (
            <div className="mt-2 flex gap-2">
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                rows={2}
                maxLength={4000}
                placeholder="Add a comment…"
                aria-label="Add a comment"
                className="flex-1 min-w-0 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5"
              />
              <button
                onClick={post}
                disabled={posting || !comment.trim()}
                aria-label="Post comment"
                className="self-end p-2 rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-40"
              >
                <Send className="w-4 h-4" />
              </button>
            </div>
          )}
        </div>
      )}

      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}

      <SignReportDialog
        studyUid={studyUid}
        open={signOpen}
        onClose={() => setSignOpen(false)}
        onSigned={(next) => { setState(next); onChanged?.(); }}
      />
    </div>
  );
}
