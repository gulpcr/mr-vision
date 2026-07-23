"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useLocale } from "@/lib/i18n";

interface SignOffActionProps {
  studyUid: string;
  onSigned?: () => void;
}

// The one sign-off entry point every report surface uses — wraps the existing
// api.reading.sign() call behind a mandatory confirm step (today it's a bare
// button with zero confirmation). No new backend endpoint.
export function SignOffAction({ studyUid, onSigned }: SignOffActionProps) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { strings } = useLocale();

  return (
    <div className="text-center">
      <button
        onClick={() => setOpen(true)}
        className="px-4 py-2 text-sm font-medium text-white bg-green-600 hover:bg-green-700 rounded-lg transition-colors"
      >
        {strings.reportShell.signOffReport}
      </button>
      {error && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{error}</p>}
      <ConfirmDialog
        tier="modal"
        open={open}
        title={strings.reportShell.signOffReport}
        consequence={strings.reportShell.signOffConsequence}
        confirmLabel={strings.reportShell.signOffReport}
        onConfirm={async () => {
          setError(null);
          try {
            await api.reading.sign(studyUid);
            setOpen(false);
            onSigned?.();
          } catch (e: any) {
            setError(e.message || "Sign off failed");
            setOpen(false);
          }
        }}
        onCancel={() => setOpen(false)}
      />
    </div>
  );
}
