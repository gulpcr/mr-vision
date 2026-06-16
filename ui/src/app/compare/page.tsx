"use client";

import { useState, useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { api, ComparisonData, Result, Study } from "@/lib/api";
import { ComparePanel } from "@/components/ComparePanel";
import { formatDate, formatPatientName } from "@/lib/format";
import { ArrowLeftRight, Loader2 } from "lucide-react";

interface StudyWithResults {
  study: Study;
  results: Result[];
}

function ComparePageInner() {
  const searchParams = useSearchParams();
  const presetA = searchParams.get("a") ?? "";
  const presetB = searchParams.get("b") ?? "";

  const [studyData, setStudyData] = useState<StudyWithResults[]>([]);
  const [loadingStudies, setLoadingStudies] = useState(true);

  const [selectedIdA, setSelectedIdA] = useState(presetA);
  const [selectedIdB, setSelectedIdB] = useState(presetB);

  const [comparison, setComparison] = useState<ComparisonData | null>(null);
  const [comparing, setComparing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load all studies + their results
  useEffect(() => {
    async function load() {
      try {
        const { studies } = await api.studies.list({ limit: "100" });
        const entries: StudyWithResults[] = [];
        for (const study of studies) {
          try {
            const { results } = await api.results.listByStudy(study.study_instance_uid);
            if (results.length > 0) entries.push({ study, results });
          } catch {}
        }
        setStudyData(entries);
      } finally {
        setLoadingStudies(false);
      }
    }
    load();
  }, []);

  // Auto-run if both presets are provided
  useEffect(() => {
    if (presetA && presetB) runCompare(presetA, presetB);
  }, [presetA, presetB]);

  // Collect all (result_id, label) pairs for dropdowns
  const allResults: { id: string; label: string; usecase: string }[] = studyData.flatMap(
    ({ study, results }) =>
      results.map((r) => ({
        id: r.id,
        usecase: r.usecase_name,
        label: `${formatPatientName(study.patient_name)} · ${formatDate(study.study_date)} · ${r.usecase_name.replace(/_/g, " ")} v${r.version}`,
      }))
  );

  const usecaseA = allResults.find((r) => r.id === selectedIdA)?.usecase ?? null;
  // Filter B to same use case as A when A is selected
  const resultsForB = usecaseA
    ? allResults.filter((r) => r.usecase === usecaseA && r.id !== selectedIdA)
    : allResults.filter((r) => r.id !== selectedIdA);

  async function runCompare(idA: string, idB: string) {
    if (!idA || !idB) return;
    setComparing(true);
    setError(null);
    setComparison(null);
    try {
      const data = await api.results.compare(idA, idB);
      setComparison(data);
    } catch (e: any) {
      setError(e.message ?? "Comparison failed");
    } finally {
      setComparing(false);
    }
  }

  const labelA =
    allResults.find((r) => r.id === selectedIdA)?.label.split(" · ").slice(0, 2).join(" · ") ??
    "Study A";
  const labelB =
    allResults.find((r) => r.id === selectedIdB)?.label.split(" · ").slice(0, 2).join(" · ") ??
    "Study B";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <ArrowLeftRight className="w-6 h-6 text-primary-600" />
        <h1 className="text-2xl font-bold text-gray-900">Compare Reports</h1>
      </div>

      {/* Selector card */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5">
              Result A
            </label>
            {loadingStudies ? (
              <div className="h-10 bg-gray-100 rounded-lg animate-pulse" />
            ) : (
              <select
                value={selectedIdA}
                onChange={(e) => {
                  setSelectedIdA(e.target.value);
                  setSelectedIdB("");
                  setComparison(null);
                }}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
              >
                <option value="">— Select a result —</option>
                {allResults.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div>
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5">
              Result B {usecaseA && <span className="text-gray-400 normal-case font-normal">(filtered to {usecaseA.replace(/_/g, " ")})</span>}
            </label>
            {loadingStudies ? (
              <div className="h-10 bg-gray-100 rounded-lg animate-pulse" />
            ) : (
              <select
                value={selectedIdB}
                onChange={(e) => {
                  setSelectedIdB(e.target.value);
                  setComparison(null);
                }}
                disabled={!selectedIdA}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:opacity-50"
              >
                <option value="">— Select a result —</option>
                {resultsForB.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={() => runCompare(selectedIdA, selectedIdB)}
            disabled={!selectedIdA || !selectedIdB || comparing}
            className="flex items-center gap-2 px-5 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {comparing ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <ArrowLeftRight className="w-4 h-4" />
            )}
            {comparing ? "Comparing…" : "Compare"}
          </button>
          {error && <p className="text-sm text-red-600">{error}</p>}
        </div>
      </div>

      {/* Comparison result */}
      {comparison && (
        <ComparePanel data={comparison} labelA={labelA} labelB={labelB} />
      )}
    </div>
  );
}

export default function ComparePage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-gray-400">Loading…</div>}>
      <ComparePageInner />
    </Suspense>
  );
}
