// EN string catalog — source of truth for keys. ur.ts must mirror this shape
// exactly (same keys, Urdu values) so `strings.foo.bar` type-checks in both.
// Scope: the primary clinical flow (nav → Worklist → Study Reader → Report),
// per the redesign brief's RTL-scaffolding requirement — not every string in
// the app. Other screens keep hardcoded English strings for now.
// Explicit shape (leaves widened to `string`) so ur.ts can assign different
// string literals for the same keys — `as const` would freeze en.ts's own
// literal values as the type, which ur.ts's Urdu text could never satisfy.
export interface Strings {
  nav: {
    dashboard: string;
    worklist: string;
    remoteReading: string;
    reports: string;
    uploadDicom: string;
  };
  worklist: {
    title: string;
    searchPlaceholder: string;
    noStudiesYet: string;
    noStudiesMatch: string;
    noStudiesYetDescription: string;
    noStudiesMatchDescription: string;
    clearFilters: string;
    uploadCta: string;
    columnPriority: string;
    columnPatient: string;
    columnStudy: string;
    columnDate: string;
    columnBodyPart: string;
    columnAiStatus: string;
  };
  statusBadge: {
    unclaimed: string;
    reading: string;
    reported: string;
    signedOff: string;
    preliminary: string;
  };
  reportShell: {
    signOffReport: string;
    signOffConsequence: string;
    radiologistOnly: string;
  };
  aiProvenance: {
    disclaimer: string;
  };
}

export const en: Strings = {
  nav: {
    dashboard: "Dashboard",
    worklist: "Worklist",
    remoteReading: "Remote Reading",
    reports: "Reports",
    uploadDicom: "Upload DICOM",
  },
  worklist: {
    title: "Worklist",
    searchPlaceholder: "Search patient, MRN, description, accession…  (/)",
    noStudiesYet: "No studies yet",
    noStudiesMatch: "No studies match your filters",
    noStudiesYetDescription: "Upload DICOM studies to get started",
    noStudiesMatchDescription: "Try adjusting or clearing your filters",
    clearFilters: "Clear filters",
    uploadCta: "Upload DICOM",
    columnPriority: "Priority",
    columnPatient: "Patient",
    columnStudy: "Study",
    columnDate: "Date",
    columnBodyPart: "Body Part",
    columnAiStatus: "AI Status",
  },
  statusBadge: {
    unclaimed: "Unclaimed",
    reading: "Reading",
    reported: "Reported",
    signedOff: "Signed off",
    preliminary: "PRELIMINARY",
  },
  reportShell: {
    signOffReport: "Sign Off Report",
    signOffConsequence:
      "Signing off finalizes this study's reading status as complete and is recorded against your account. This action cannot be undone from this screen.",
    radiologistOnly: "Only a radiologist or admin can sign off this report.",
  },
  aiProvenance: {
    disclaimer:
      "This is an AI-generated analysis intended to assist clinical decision-making. It is not a substitute for professional medical judgment. All findings must be reviewed and validated by a qualified radiologist or physician before clinical use.",
  },
};
