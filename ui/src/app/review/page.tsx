"use client";

import { useState } from "react";
import { useReviewQueue } from "@/lib/hooks";
import Link from "next/link";
import { Eye, AlertTriangle } from "lucide-react";

export default function ReviewPage() {
  const [statusFilter, setStatusFilter] = useState("pending");
  const params: Record<string, string> = {};
  if (statusFilter) params.status = statusFilter;

  const { data, isLoading } = useReviewQueue(params);

  const getConfidenceColor = (score: number) => {
    if (score < 0.3) return "text-red-600 bg-red-50";
    if (score < 0.5) return "text-amber-600 bg-amber-50";
    return "text-yellow-600 bg-yellow-50";
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Review Queue</h1>
          {data?.stats && (
            <div className="flex gap-3 mt-2 text-sm">
              <span className="text-amber-600">Pending: {data.stats.pending || 0}</span>
              <span className="text-green-600">Approved: {data.stats.approved || 0}</span>
              <span className="text-red-600">Rejected: {data.stats.rejected || 0}</span>
            </div>
          )}
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="text-sm border border-gray-200 rounded-lg px-3 py-2"
        >
          <option value="">All</option>
          <option value="pending">Pending</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
        </select>
      </div>

      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        {isLoading ? <div className="p-12 text-center text-gray-400">Loading...</div> : (
          <table className="w-full text-sm">
            <thead><tr className="border-b border-gray-100">
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Study</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Use Case</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Confidence</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Status</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Reviewer</th>
              <th className="py-3 px-4"></th>
            </tr></thead>
            <tbody className="divide-y divide-gray-50">
              {(data?.items || []).map((item) => (
                <tr key={item.id} className="hover:bg-gray-50">
                  <td className="py-2.5 px-4">
                    <div className="flex items-center gap-2">
                      <AlertTriangle className="w-4 h-4 text-amber-500" />
                      <span className="text-gray-700 font-mono text-xs">{item.study_instance_uid.slice(0, 20)}...</span>
                    </div>
                  </td>
                  <td className="py-2.5 px-4 text-gray-600">{item.usecase_name.replace(/_/g, " ")}</td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${getConfidenceColor(item.confidence_score)}`}>
                      {(item.confidence_score * 100).toFixed(1)}%
                    </span>
                  </td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${
                      item.status === "approved" ? "bg-green-50 text-green-700" :
                      item.status === "rejected" ? "bg-red-50 text-red-700" :
                      "bg-amber-50 text-amber-700"
                    }`}>{item.status}</span>
                  </td>
                  <td className="py-2.5 px-4 text-gray-500">{item.reviewer || "-"}</td>
                  <td className="py-2.5 px-4">
                    <Link href={`/review/${item.id}`} className="flex items-center gap-1 text-primary-600 hover:text-primary-700 text-xs font-medium">
                      <Eye className="w-3.5 h-3.5" /> Review
                    </Link>
                  </td>
                </tr>
              ))}
              {(!data?.items || data.items.length === 0) && (
                <tr><td colSpan={6} className="py-12 text-center text-gray-400">No items in review queue</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
