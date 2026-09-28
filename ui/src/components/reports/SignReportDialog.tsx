"use client";

import { useEffect, useState } from "react";
import { api, type SignoffState } from "@/lib/api";
import { Modal } from "@/components/ui/Modal";
import { PriorityBadge } from "./PriorityBadge";
import { PenLine } from "lucide-react";

interface SignReportDialogProps {
  studyUid: string;
  open: boolean;
  onClose: () => void;
  onSigned?: (state: SignoffState) => void;
}

// Electronic signature: the signer reads the attestation statement (with their name in
// it), writes a mandatory comment and ticks the affirmation before Sign is enabled.
export function SignReportDialog({ studyUid, open, onClose, onSigned }: SignReportDialogProps) {
  const [state, setState] = useState<SignoffState | null>(null);
  const [comment, setComment] = useState("");
  const [fullName, setFullName] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setComment("");
    setAgreed(false);
    setError(null);
    setState(null);
    api.signoff.get(studyUid).then(setState).catch((e) => setError(e.message || "Failed to load"));
  }, [open, studyUid]);

  const needsName = !!state && !state.statement.signer_full_name;
  const name = needsName ? fullName.trim() : state?.statement.signer_full_name ?? "";
  const statementText = state
    ? state.statement.template.replace("{full_name}", name || "[your full name]")
    : "";
  const canSign = !!state && agreed && comment.trim().length > 0 && (!needsName || name.length >= 3) && !busy;

  const sign = async () => {
    if (!state) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.signoff.sign(studyUid, {
        statement_version: state.statement.version,
        agreed,
        comment: comment.trim(),
        ...(needsName ? { full_name: name } : {}),
      });
      onSigned?.(next);
      onClose();
    } catch (e: any) {
      setError(e.message || "Signing failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Electronically sign report"
      size="lg"
      footer={
        <div className="flex items-center justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1.5 text-sm rounded-lg text-gray-600 dark:text-gray-300 hover:bg-black/5 dark:hover:bg-white/5">
            Cancel
          </button>
          <button
            onClick={sign}
            disabled={!canSign}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-semibold rounded-lg text-white bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <PenLine className="w-4 h-4" /> {busy ? "Signing…" : "Sign report"}
          </button>
        </div>
      }
    >
      {!state && !error && <p className="p-4 text-sm text-gray-500">Loading…</p>}
      {state && (
        <div className="space-y-4 p-1">
          <div className="flex flex-wrap items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
            <span>Priority:</span>
            <PriorityBadge priority={state.priority} overridden={state.priority_overridden} />
            {state.priority_reasons.length > 0 && (
              <span className="text-xs text-gray-500 dark:text-gray-400">{state.priority_reasons.join(" · ")}</span>
            )}
          </div>

          {needsName && (
            <label className="block text-sm">
              <span className="font-medium text-gray-700 dark:text-gray-200">Your full name</span>
              <span className="block text-xs text-gray-500 dark:text-gray-400">
                Your profile has no full name yet. It will be saved and used on this and future signatures.
              </span>
              <input
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                maxLength={256}
                placeholder="e.g. Dr. Ayesha Khan"
                className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
              />
            </label>
          )}

          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-1">
              Attestation
            </p>
            <blockquote className="text-sm leading-relaxed text-gray-800 dark:text-gray-100 bg-gray-50 dark:bg-surface-raised border-l-4 border-emerald-500 rounded-r-lg px-4 py-3">
              {statementText}
            </blockquote>
          </div>

          <label className="block text-sm">
            <span className="font-medium text-gray-700 dark:text-gray-200">
              Signing comment <span className="text-red-600">*</span>
            </span>
            <span className="block text-xs text-gray-500 dark:text-gray-400">
              Your interpretation or verification note — e.g. findings confirmed or corrected, recommendations.
            </span>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              rows={4}
              maxLength={4000}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>

          <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-200">
            <input type="checkbox" className="mt-1" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />
            <span>I have read the statement above and I affirm it. I am signing this report electronically.</span>
          </label>
        </div>
      )}
      {error && <p className="px-1 pt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </Modal>
  );
}
