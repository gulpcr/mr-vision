"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { mutate } from "swr";
import { useStudy, useResult } from "@/lib/hooks";
import { resolveReportKind } from "@/components/reports/registry";
import { AbdomenCtContent } from "@/components/reports/content/AbdomenCtContent";
import { MammographyContent } from "@/components/reports/content/MammographyContent";
import { MolecularContent } from "@/components/reports/content/MolecularContent";

// Single dynamic route replacing the 4 former near-identical wrapper pages
// (study/[uid]/mri, /abdomen, /mammography, /molecular) — looks up which
// content renderer handles this usecase via the registry. The old 4 routes
// now redirect here (see their page.tsx files).
export default function UnifiedReportPage() {
  const params = useParams();
  const uid = params.uid as string;
  const usecase = params.usecase as string;
  const kind = resolveReportKind(usecase);

  const { data: study, isLoading: studyLoading } = useStudy(uid);
  // Mammography's result is optional (report can be pending); every other kind
  // requires a real result to render.
  const { data: result, isLoading: resultLoading, error } = useResult(uid, kind === "mammography" ? "mammography" : usecase);

  const onSignedOff = () => mutate(["study", uid]);

  const loading = studyLoading || (kind !== "mammography" && resultLoading);

  return (
    <div className="p-4">
      <div className="no-print mb-4">
        <Link
          href={`/study/${uid}`}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
        >
          <ArrowLeft className="w-4 h-4" /> Back to study
        </Link>
      </div>

      {loading ? (
        <p className="text-center text-sm text-gray-400 py-12">Loading report…</p>
      ) : !kind ? (
        <p className="text-center text-sm text-red-500 py-12">No report is available for this use case.</p>
      ) : !study ? (
        <p className="text-center text-sm text-red-500 py-12">Study not found.</p>
      ) : kind === "mammography" ? (
        <MammographyContent study={study} result={result ?? null} onSignedOff={onSignedOff} />
      ) : error || !result ? (
        <p className="text-center text-sm text-red-500 py-12">Report not found for this study / use case.</p>
      ) : kind === "molecular" ? (
        <MolecularContent study={study} result={result} onSignedOff={onSignedOff} />
      ) : (
        <AbdomenCtContent study={study} result={result} onSignedOff={onSignedOff} />
      )}
    </div>
  );
}
