"use client";

import { useState } from "react";
import { useRoutingRules } from "@/lib/hooks";
import { api } from "@/lib/api";
import { GitBranch } from "lucide-react";

export default function RoutingAdminPage() {
  const { data: rulesData, isLoading, mutate } = useRoutingRules();
  const [editing, setEditing] = useState(false);
  const [editJson, setEditJson] = useState("");

  const rules = rulesData?.routing_rules || {};

  const handleEdit = () => {
    setEditJson(JSON.stringify([], null, 2));
    setEditing(true);
  };

  const handleSave = async () => {
    try {
      const parsed = JSON.parse(editJson);
      await api.admin.updateRoutingRules(parsed);
      setEditing(false);
      mutate();
    } catch (e: any) {
      alert(`Save failed: ${e.message}`);
    }
  };

  if (isLoading) return <p className="text-gray-500 p-4">Loading...</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <GitBranch className="w-7 h-7 text-primary-600" />
          <h1 className="text-2xl font-bold text-gray-900">Routing Rules</h1>
        </div>
        <button
          onClick={editing ? handleSave : handleEdit}
          className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          {editing ? "Save Site Overrides" : "Edit Site Overrides"}
        </button>
      </div>

      {editing && (
        <div className="mb-6 bg-white rounded-lg shadow-sm border border-gray-200 p-5">
          <h3 className="font-medium text-gray-900 mb-2">Site Routing Override (JSON)</h3>
          <textarea
            value={editJson}
            onChange={(e) => setEditJson(e.target.value)}
            className="w-full h-48 font-mono text-xs border border-gray-200 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-primary-500"
            placeholder='[{"usecase_name": "brain_mri", "body_parts": ["BRAIN"], "priority": 100}]'
          />
          <p className="text-xs text-gray-400 mt-2">
            Enter an array of routing rule override objects.
          </p>
        </div>
      )}

      <div className="space-y-4">
        {Object.entries(rules).map(([ucName, ucRules]) => (
          <div key={ucName} className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
            <h2 className="font-semibold text-gray-900 mb-3">
              {ucName.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
            </h2>
            {(ucRules as any[]).map((rule: any, idx: number) => (
              <div key={idx} className="border border-gray-100 rounded-lg p-3 mb-2 text-sm">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <span className="text-xs text-gray-500 uppercase tracking-wider">Body Parts</span>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {(rule.body_parts || []).map((bp: string) => (
                        <span key={bp} className="bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full text-xs font-medium">
                          {bp}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div>
                    <span className="text-xs text-gray-500 uppercase tracking-wider">Modality</span>
                    <p className="text-xs mt-1 text-gray-700">{rule.modality || "MR"}</p>
                  </div>
                  {rule.study_description_patterns?.length > 0 && (
                    <div className="col-span-2">
                      <span className="text-xs text-gray-500 uppercase tracking-wider">Study Patterns</span>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {rule.study_description_patterns.map((p: string, i: number) => (
                          <code key={i} className="bg-gray-50 border border-gray-200 px-2 py-0.5 rounded text-xs">
                            {p}
                          </code>
                        ))}
                      </div>
                    </div>
                  )}
                  {rule.series_description_patterns?.length > 0 && (
                    <div className="col-span-2">
                      <span className="text-xs text-gray-500 uppercase tracking-wider">Series Patterns</span>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {rule.series_description_patterns.map((p: string, i: number) => (
                          <code key={i} className="bg-gray-50 border border-gray-200 px-2 py-0.5 rounded text-xs">
                            {p}
                          </code>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-4 text-xs text-gray-400">
                  <span>Priority: {rule.priority || 0}</span>
                  <span
                    className={
                      rule.enabled !== false ? "text-green-600" : "text-gray-400"
                    }
                  >
                    {rule.enabled !== false ? "Enabled" : "Disabled"}
                  </span>
                </div>
              </div>
            ))}
            {(ucRules as any[]).length === 0 && (
              <p className="text-sm text-gray-400">No routing rules defined</p>
            )}
          </div>
        ))}
        {Object.keys(rules).length === 0 && (
          <div className="text-center py-12 text-gray-400">
            <GitBranch className="w-10 h-10 mx-auto mb-2 text-gray-300" />
            No routing rules configured
          </div>
        )}
      </div>
    </div>
  );
}
