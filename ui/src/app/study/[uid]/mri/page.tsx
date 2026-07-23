import { redirect } from "next/navigation";

// Redirect shim. MriReport.tsx (formerly rendered here) was confirmed
// unreachable dead code — no link anywhere in the frontend or backend ever
// pointed to this route; study/[uid]/page.tsx's "AI Report" link has always
// routed CT+MRI use cases to /abdomen (now /report/[usecase]) instead, via
// isCtReportUsecase(). This shim exists only as a safety net for any stray
// external link — it resolves through the same unified route/registry.
export default function MriReportRedirect({
  params,
  searchParams,
}: {
  params: { uid: string };
  searchParams: { usecase?: string };
}) {
  redirect(searchParams.usecase ? `/study/${params.uid}/report/${searchParams.usecase}` : `/study/${params.uid}`);
}
