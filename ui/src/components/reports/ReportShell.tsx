"use client";

import type { Study } from "@/lib/api";
import { ReportSignoffPanel } from "./ReportSignoffPanel";

interface ReportShellProps {
  study: Study;
  /** Toolbar buttons (Download PDF / Print) — each report supplies its own, since the download action differs per use case (or is absent, e.g. PET-CT has no PDF export today). */
  toolbar: React.ReactNode;
  /** Rendered above the bordered document card — e.g. Mammography's amber AI-callout + its own PDF-error banner. */
  topBanner?: React.ReactNode;
  /** Rendered inside the card, near the bottom — the footer-style AIProvenanceBanner used by Mri/AbdomenCt/Molecular. */
  footerBanner?: React.ReactNode;
  /** Signatory block — shape varies (single/dual/none); omit entirely for reports that don't show one (e.g. Mammography today). */
  signatory?: React.ReactNode;
  showComputerGeneratedNote?: boolean;
  containerClassName?: string;
  onSignedOff?: () => void;
  children: React.ReactNode;
}

const DEFAULT_CONTAINER_CLASS =
  "border border-gray-300 dark:border-gray-700 rounded-lg p-8 text-sm leading-relaxed print:border-0 print:p-0";

// The shared shell every narrative report renders through — owns the outer
// document wrapper, toolbar row, banner placement, signatory area, and the
// sign-off action. Each use case's *content* (demographics, findings,
// scoring) stays in its own content/ renderer and is passed as children —
// deliberately NOT forced into a single generic shape, since the 4 reports'
// demographics tables and signatory blocks are genuinely different data, not
// just styling (see ARCHITECTURE.md).
export function ReportShell({
  study,
  toolbar,
  topBanner,
  footerBanner,
  signatory,
  showComputerGeneratedNote = false,
  containerClassName = DEFAULT_CONTAINER_CLASS,
  onSignedOff,
  children,
}: ReportShellProps) {
  const signed = (study.reading_status || "unread") === "signed";

  return (
    <div className="mx-auto max-w-3xl bg-white dark:bg-surface text-gray-900 dark:text-gray-100">
      <div className="no-print flex justify-end gap-2 mb-3">{toolbar}</div>

      {topBanner}

      <div className={containerClassName}>
        {children}

        {footerBanner}

        {signatory}

        {showComputerGeneratedNote && (
          <p className="text-center text-[11px] italic font-bold text-gray-500 dark:text-gray-400 mt-10 pt-3 border-t border-gray-100 dark:border-gray-700">
            {signed
              ? "Note: This report has been electronically signed; the signature below is legally binding."
              : "Note: This is a computer generated document and does not require any signature."}
          </p>
        )}

        {/* Priority, electronic signature (or the Sign button) and comments. */}
        <ReportSignoffPanel
          studyUid={study.study_instance_uid}
          onChanged={onSignedOff}
          className="mt-8 pt-4 border-t border-gray-100 dark:border-gray-700"
        />
      </div>
    </div>
  );
}
