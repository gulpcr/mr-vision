"use client";

import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useStudy, useResult } from "@/lib/hooks";
import { AbdomenCtReport } from "@/components/AbdomenCtReport";

export default function AbdomenCtReportPage() {
  const params = useParams();
  const search = useSearchParams();
  const uid = params.uid as string;
  const usecase = search.get("usecase") || "abdomen_ct";

  const { data: study, isLoading: studyLoading } = useStudy(uid);
  const { data: result, isLoading: resultLoading, error } = useResult(uid, usecase);

  return (
    <div className="p-4">
      <div className="no-print mb-4">
        <Link
          href={`/study/${uid}`}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-600 hover:text-gray-900"
        >
          <ArrowLeft className="w-4 h-4" /> Back to study
        </Link>
      </div>

      {studyLoading || resultLoading ? (
        <p className="text-center text-sm text-gray-400 py-12">Loading report…</p>
      ) : error || !result || !study ? (
        <p className="text-center text-sm text-red-500 py-12">Report not found for this study / use case.</p>
      ) : (
        <AbdomenCtReport study={study} result={result} />
      )}
    </div>
  );
}
