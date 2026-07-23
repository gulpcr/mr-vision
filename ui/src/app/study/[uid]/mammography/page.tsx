import { redirect } from "next/navigation";

// Redirect shim — superseded by the unified /study/[uid]/report/[usecase]
// route. Kept so bookmarked/emailed links to this path still resolve.
export default function MammographyReportRedirect({ params }: { params: { uid: string } }) {
  redirect(`/study/${params.uid}/report/mammography`);
}
