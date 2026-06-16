"use client";

import { useState } from "react";
import { useAuditLogs } from "@/lib/hooks";
import { Search, Filter, Clock } from "lucide-react";

export default function AuditPage() {
  const [actionFilter, setActionFilter] = useState("");
  const [entityFilter, setEntityFilter] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const params: Record<string, string> = {
    offset: String(offset),
    limit: String(limit),
  };
  if (actionFilter) params.action = actionFilter;
  if (entityFilter) params.entity_type = entityFilter;
  if (actorFilter) params.actor = actorFilter;

  const { data, isLoading } = useAuditLogs(params);

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Audit Log</h1>

      {/* Filters */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 mb-4">
        <div className="flex flex-wrap items-center gap-3 px-4 py-3">
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-gray-400" />
            <select
              value={actionFilter}
              onChange={(e) => { setActionFilter(e.target.value); setOffset(0); }}
              className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="">All Actions</option>
              <option value="study_received">Study Received</option>
              <option value="job_created">Job Created</option>
              <option value="job_completed">Job Completed</option>
              <option value="job_failed">Job Failed</option>
              <option value="job_cancelled">Job Cancelled</option>
              <option value="config_changed">Config Changed</option>
              <option value="user_login">User Login</option>
            </select>
          </div>
          <input
            type="text"
            value={entityFilter}
            onChange={(e) => { setEntityFilter(e.target.value); setOffset(0); }}
            placeholder="Entity type..."
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
          <input
            type="text"
            value={actorFilter}
            onChange={(e) => { setActorFilter(e.target.value); setOffset(0); }}
            placeholder="Actor..."
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center text-gray-400">Loading...</div>
        ) : (
          <>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Timestamp</th>
                  <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Action</th>
                  <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Entity</th>
                  <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Actor</th>
                  <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {(data?.entries || []).map((entry) => (
                  <tr key={entry.id} className="hover:bg-gray-50">
                    <td className="py-2.5 px-4 text-gray-500 whitespace-nowrap">
                      <div className="flex items-center gap-1.5">
                        <Clock className="w-3 h-3" />
                        {entry.timestamp ? new Date(entry.timestamp).toLocaleString() : "-"}
                      </div>
                    </td>
                    <td className="py-2.5 px-4">
                      <span className="px-2 py-0.5 bg-blue-50 text-blue-700 text-xs rounded-full font-medium">
                        {entry.action}
                      </span>
                    </td>
                    <td className="py-2.5 px-4 text-gray-700">
                      {entry.entity_type}/{entry.entity_id?.slice(0, 12)}
                    </td>
                    <td className="py-2.5 px-4 text-gray-600">{entry.actor}</td>
                    <td className="py-2.5 px-4 text-gray-500 text-xs max-w-[200px] truncate">
                      {JSON.stringify(entry.details).slice(0, 80)}
                    </td>
                  </tr>
                ))}
                {(!data?.entries || data.entries.length === 0) && (
                  <tr>
                    <td colSpan={5} className="py-12 text-center text-gray-400">
                      No audit entries found
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            <div className="px-4 py-3 bg-gray-50 text-xs text-gray-500 border-t border-gray-100 flex justify-between items-center">
              <span>Total: {data?.total || 0} entries</span>
              <div className="flex gap-2">
                <button
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                  disabled={offset === 0}
                  className="px-3 py-1 border border-gray-200 rounded text-xs disabled:opacity-50 hover:bg-gray-100"
                >
                  Previous
                </button>
                <button
                  onClick={() => setOffset(offset + limit)}
                  disabled={(data?.entries?.length || 0) < limit}
                  className="px-3 py-1 border border-gray-200 rounded text-xs disabled:opacity-50 hover:bg-gray-100"
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
