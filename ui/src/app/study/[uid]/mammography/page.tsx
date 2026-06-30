"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useStudy, useResult } from "@/lib/hooks";
import { MammographyReport } from "@/components/MammographyReport";

export default function MammographyReportPage() {
  const params = useParams();
  const uid = params.uid as string;

  const { data: study, isLoading: studyLoading } = useStudy(uid);
  // The AI result pre-fills the report; it's optional — the report is editable
  // even before an AI run.
  const { data: result } = useResult(uid, "mammography");

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

      {studyLoading || !study ? (
        <p className="text-center text-sm text-gray-400 py-12">Loading report…</p>
      ) : (
        <MammographyReport study={study} result={result ?? null} />
      )}
    </div>
  );
}
