import { isCtReportUsecase } from "@/lib/ctReport";

export type ReportKind = "ct-mri" | "mammography" | "molecular";

const MOLECULAR_USECASES = new Set(["pet_ct", "pet_ct_brain"]);

// Classifies a use case name to the content renderer that actually handles it
// today. Note: isCtReportUsecase() covers BOTH the CT family (abdomen_ct, ct_*)
// AND all *_mri plugins — lib/ctReport.ts's CT_REGION_META already has correct
// entries for all of them, which is what study/[uid]/page.tsx's "AI Report"
// link has always routed to (via the old /abdomen route). There is no
// separate live MRI-only report path — see ARCHITECTURE.md.
export function resolveReportKind(usecase: string | null | undefined): ReportKind | null {
  if (!usecase) return null;
  if (usecase === "mammography") return "mammography";
  if (MOLECULAR_USECASES.has(usecase)) return "molecular";
  if (isCtReportUsecase(usecase)) return "ct-mri";
  return null;
}
