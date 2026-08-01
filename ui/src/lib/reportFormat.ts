// Shared demographics formatters for narrative reports — extracted verbatim from
// the (byte-identical) copies previously duplicated across MriReport.tsx,
// AbdomenCtReport.tsx, MammographyReport.tsx, and MolecularReport.tsx's
// fmtReportDate. Deliberately separate from lib/format.ts, which formats dates
// differently ("Jan 5, 2026") for non-report screens — do not merge the two.

export function fmtAge(raw: string | null): string {
  if (!raw) return "—";
  const m = String(raw).trim().match(/^(\d{3})([YMWD])$/i);
  if (m) {
    const unit = { Y: "Yrs", M: "Mos", W: "Wks", D: "Days" }[m[2].toUpperCase() as "Y" | "M" | "W" | "D"];
    return `${parseInt(m[1], 10)} ${unit}`;
  }
  return String(raw);
}

// Accepts BOTH the legacy raw DICOM codes ("M"/"F"/"O") and the normalised FHIR
// administrativeGender values the backend now stores ("male"/"female"/"other"/
// "unknown"), so studies ingested before and after that change render identically.
export function fmtSex(raw: string | null): string {
  if (!raw) return "—";
  return (
    {
      M: "Male", F: "Female", O: "Other",
      MALE: "Male", FEMALE: "Female", OTHER: "Other", UNKNOWN: "Unknown",
    } as Record<string, string>
  )[raw.trim().toUpperCase()] || raw;
}

export function fmtDate(raw: string | null): string {
  if (!raw) return "—";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return raw;
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}/${d.getFullYear()}`;
}
