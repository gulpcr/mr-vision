"use client";

import { useState, useEffect } from "react";
import { useStudies, useUsecases } from "@/lib/hooks";
import { api, Result, Study } from "@/lib/api";
import { formatDate, formatDateTime, formatPatientName } from "@/lib/format";
import Link from "next/link";
import { FileText, Search, Filter, ArrowLeftRight } from "lucide-react";

interface ReportEntry {
  study: Study;
  result: Result;
  selected?: boolean;
}

export default function ReportsPage() {
  const { data: studyData, isLoading: studiesLoading } = useStudies({ limit: "50" });
  const { data: usecases } = useUsecases();
  const [entries, setEntries] = useState<ReportEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [usecaseFilter, setUsecaseFilter] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  function toggleSelect(id: string) {
    setSelectedIds((prev) =>
      prev.includes(id)
        ? prev.filter((x) => x !== id)
        : prev.length < 2
        ? [...prev, id]
        : [prev[1], id]
    );
  }

  useEffect(() => {
    if (!studyData?.studies?.length) {
      setLoading(false);
      return;
    }

    async function loadResults() {
      const results: ReportEntry[] = [];
      for (const study of studyData!.studies) {
        try {
          const { results: studyResults } = await api.results.listByStudy(
            study.study_instance_uid
          );
          // Backend already returns is_latest=true only, but deduplicate
          // defensively by usecase_name keeping highest version number.
          const latestByUsecase = new Map<string, Result>();
          for (const r of studyResults) {
            const existing = latestByUsecase.get(r.usecase_name);
            if (!existing || r.version > existing.version) {
              latestByUsecase.set(r.usecase_name, r);
            }
          }
          Array.from(latestByUsecase.values()).forEach((r) => {
            results.push({ study, result: r });
          });
        } catch {}
      }
      results.sort(
        (a, b) =>
          new Date(b.result.created_at).getTime() -
          new Date(a.result.created_at).getTime()
      );
      setEntries(results);
      setLoading(false);
    }
    loadResults();
  }, [studyData]);

  const filtered = entries.filter((e) => {
    const q = search.toLowerCase();
    const matchSearch =
      !q ||
      formatPatientName(e.study.patient_name).toLowerCase().includes(q) ||
      e.study.patient_id?.toLowerCase().includes(q) ||
      e.result.usecase_name.toLowerCase().includes(q);
    const matchUsecase =
      !usecaseFilter || e.result.usecase_name === usecaseFilter;
    return matchSearch && matchUsecase;
  });

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Reports</h1>

      {selectedIds.length === 2 && (
        <div className="bg-primary-50 border border-primary-200 rounded-lg px-4 py-3 flex items-center justify-between">
          <p className="text-sm text-primary-700 font-medium">2 reports selected</p>
          <Link
            href={`/compare?a=${selectedIds[0]}&b=${selectedIds[1]}`}
            className="flex items-center gap-1.5 px-4 py-1.5 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 transition-colors"
          >
            <ArrowLeftRight className="w-4 h-4" />
            Compare
          </Link>
        </div>
      )}

      <div className="bg-white rounded-lg shadow-sm border border-gray-200">
        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3 px-5 py-3 border-b border-gray-100">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by patient or use case..."
              className="w-full pl-9 pr-4 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-gray-400" />
            <select
              value={usecaseFilter}
              onChange={(e) => setUsecaseFilter(e.target.value)}
              className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="">All Use Cases</option>
              {usecases?.map((uc) => (
                <option key={uc.name} value={uc.name}>
                  {uc.name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Table */}
        {studiesLoading || loading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading reports...</div>
        ) : filtered.length === 0 ? (
          <div className="p-8 text-center text-sm text-gray-400">
            <FileText className="w-8 h-8 mx-auto mb-2 text-gray-300" />
            No completed reports found
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="px-4 py-2.5 w-10"></th>
                  <th className="text-left px-5 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Patient
                  </th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Study Date
                  </th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Use Case
                  </th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Model Version
                  </th>
                  <th className="text-left px-3 py-2.5 text-xs font-semibold text-gray-500 uppercase">
                    Generated
                  </th>
                  <th className="px-5 py-2.5"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {filtered.map((e) => (
                  <tr
                    key={`${e.result.id}`}
                    className="hover:bg-gray-50"
                  >
                    <td className="px-4 py-3">
                      <input
                        type="checkbox"
                        checked={selectedIds.includes(e.result.id)}
                        onChange={() => toggleSelect(e.result.id)}
                        title="Select for comparison"
                        className="w-4 h-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500 cursor-pointer"
                      />
                    </td>
                    <td className="px-5 py-3">
                      <p className="font-medium text-gray-900">
                        {formatPatientName(e.study.patient_name)}
                      </p>
                      <p className="text-xs text-gray-500">{e.study.patient_id}</p>
                    </td>
                    <td className="px-3 py-3 text-gray-600">
                      {formatDate(e.study.study_date)}
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-1.5">
                        <span className="px-2 py-0.5 bg-primary-50 text-primary-700 text-xs rounded-full font-medium">
                          {e.result.usecase_name.replace(/_/g, " ")}
                        </span>
                        {e.result.version > 1 && (
                          <span className="px-1.5 py-0.5 bg-amber-50 text-amber-600 text-[10px] rounded font-semibold border border-amber-100">
                            v{e.result.version}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-3 text-gray-600">{e.result.model_version}</td>
                    <td className="px-3 py-3 text-gray-600 text-xs">
                      {formatDateTime(e.result.created_at)}
                    </td>
                    <td className="px-5 py-3">
                      <Link
                        href={`/study/${e.study.study_instance_uid}`}
                        className="text-xs font-medium text-primary-600 hover:text-primary-700"
                      >
                        View Report
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
