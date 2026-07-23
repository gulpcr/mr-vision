interface DiffViewProps {
  aiText?: string | null;
  editedText?: string | null;
}

// Contract-only for now: no backend field/table anywhere stores an AI-text /
// edited-text pair for any of these reports (verified — the one candidate,
// mammography-report PUT, is a mutually-exclusive alternate manual-dictation
// path, hard-rejected when AI mode is on, so it would be actively misleading to
// repurpose as "the edited version"). Renders the honest "no edits" state until
// a real report_edits entity + endpoint exists as a follow-up; not wired into
// ReportShell by default since every report renders this identically today.
export function DiffView({ aiText, editedText }: DiffViewProps) {
  if (!editedText) {
    return (
      <div className="text-xs text-gray-400 dark:text-gray-500 italic border border-dashed border-gray-300 dark:border-gray-700 rounded-lg px-4 py-3 text-center">
        No clinician edits recorded for this report.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-4 text-sm">
      <div>
        <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
          AI-generated
        </p>
        <p className="whitespace-pre-line text-gray-500 dark:text-gray-400">{aiText}</p>
      </div>
      <div>
        <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
          Clinician-edited
        </p>
        <p className="whitespace-pre-line text-gray-900 dark:text-gray-100 font-medium">{editedText}</p>
      </div>
    </div>
  );
}
