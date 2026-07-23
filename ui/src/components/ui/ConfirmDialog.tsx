"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { Modal } from "./Modal";

export type ConfirmTier = "inline" | "modal" | "type-to-confirm";

export interface ConfirmDialogProps {
  tier: ConfirmTier;
  /** Ignored for tier="inline" — the parent controls whether it's mounted at all. */
  open?: boolean;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
  /** Required for "modal" / "type-to-confirm" (rendered as the dialog title); unused by "inline". */
  title?: string;
  /** Required for "modal" / "type-to-confirm" — the consequence of proceeding. */
  consequence?: string;
  /** Required for "type-to-confirm" — the exact phrase the user must type. */
  confirmPhrase?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

// One confirmation pattern with friction scaling to blast radius (see redesign
// brief §3): inline swap-in-place for reversible/low-impact deletes, a modal with
// explicit consequence text for moderate actions, and a type-to-confirm modal for
// irreversible/high-blast-radius actions.
export function ConfirmDialog(props: ConfirmDialogProps) {
  if (props.tier === "inline") return <InlineConfirm {...props} />;
  if (props.tier === "type-to-confirm") return <TypeToConfirmModal {...props} />;
  return <ModalConfirm {...props} />;
}

function InlineConfirm({ onConfirm, onCancel, confirmLabel = "Confirm", cancelLabel = "Keep", danger }: ConfirmDialogProps) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        onClick={() => onConfirm()}
        className={`px-2 py-1 text-xs font-medium rounded transition-colors ${
          danger
            ? "text-white bg-red-600 hover:bg-red-700"
            : "text-white bg-primary-600 hover:bg-primary-700"
        }`}
      >
        {confirmLabel}
      </button>
      <button
        onClick={onCancel}
        className="px-2 py-1 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 rounded transition-colors"
      >
        {cancelLabel}
      </button>
    </span>
  );
}

function ModalConfirm({
  open = false,
  onConfirm,
  onCancel,
  title = "Confirm",
  consequence,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger,
}: ConfirmDialogProps) {
  const [busy, setBusy] = useState(false);

  const handleConfirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onCancel} title={title} size="sm">
      <div className="space-y-4">
        {consequence && (
          <div className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300">
            {danger && <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-red-500" />}
            <p>{consequence}</p>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            onClick={handleConfirm}
            disabled={busy}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg transition-colors disabled:opacity-50 ${
              danger ? "bg-red-600 hover:bg-red-700" : "bg-primary-600 hover:bg-primary-700"
            }`}
          >
            {busy && <RefreshCw className="w-4 h-4 animate-spin motion-reduce:animate-none" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}

type TypeToConfirmState = "entering" | "running" | "error";

function TypeToConfirmModal({
  open = false,
  onConfirm,
  onCancel,
  title = "Confirm",
  consequence,
  confirmPhrase = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
}: ConfirmDialogProps) {
  const [state, setState] = useState<TypeToConfirmState>("entering");
  const [text, setText] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const matches = text === confirmPhrase;

  const reset = () => {
    setState("entering");
    setText("");
    setErrorMsg(null);
  };

  const handleClose = () => {
    reset();
    onCancel();
  };

  const handleConfirm = async () => {
    if (!matches) return;
    setState("running");
    try {
      await onConfirm();
      reset();
      onCancel(); // action succeeded — close the dialog; caller renders its own result summary
    } catch (e: any) {
      setErrorMsg(e?.message || "Action failed");
      setState("error");
    }
  };

  return (
    <Modal open={open} onClose={handleClose} title={title} size="sm">
      <div className="space-y-3">
        {consequence && (
          <div className="flex items-start gap-2 text-sm text-red-700 dark:text-red-400">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
            <p>{consequence}</p>
          </div>
        )}

        {state === "entering" && (
          <>
            <p className="text-sm text-gray-700 dark:text-gray-300">
              Type <span className="font-mono font-bold text-red-700 dark:text-red-400">{confirmPhrase}</span> to
              confirm:
            </p>
            <input
              autoFocus
              type="text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={confirmPhrase}
              aria-label={`Type ${confirmPhrase} to confirm`}
              className="w-full px-3 py-2 text-sm border-2 border-red-300 dark:border-red-800 dark:bg-surface-raised rounded-lg focus:outline-none focus:border-red-500 font-mono"
              onKeyDown={(e) => {
                if (e.key === "Enter" && matches) handleConfirm();
              }}
            />
            <div className="flex gap-2">
              <button
                onClick={handleConfirm}
                disabled={!matches}
                className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg transition-colors"
              >
                {confirmLabel}
              </button>
              <button
                onClick={handleClose}
                className="px-4 py-2 text-sm font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 rounded-lg transition-colors"
              >
                {cancelLabel}
              </button>
            </div>
          </>
        )}

        {state === "running" && (
          <div className="flex items-center gap-3 text-sm text-gray-600 dark:text-gray-300">
            <RefreshCw className="w-4 h-4 animate-spin motion-reduce:animate-none text-red-500" />
            Working…
          </div>
        )}

        {state === "error" && (
          <div className="space-y-2">
            <div className="flex items-start gap-2 text-red-700 dark:text-red-400 text-sm">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>{errorMsg}</span>
            </div>
            <button
              onClick={reset}
              className="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 underline"
            >
              Try again
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}
