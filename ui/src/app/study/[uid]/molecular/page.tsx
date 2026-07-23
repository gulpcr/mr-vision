import { redirect } from "next/navigation";

// Redirect shim — superseded by the unified /study/[uid]/report/[usecase]
// route. Kept so bookmarked/emailed links to this path still resolve.
export default function MolecularReportRedirect({
  params,
  searchParams,
}: {
  params: { uid: string };
  searchParams: { usecase?: string };
}) {
  const usecase = searchParams.usecase || "pet_ct";
  redirect(`/study/${params.uid}/report/${usecase}`);
}
