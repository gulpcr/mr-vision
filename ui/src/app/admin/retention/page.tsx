"use client";

import { useState } from "react";
import { useRetentionPolicies } from "@/lib/hooks";
import { api } from "@/lib/api";
import { Plus, Trash2, Play } from "lucide-react";
import { mutate } from "swr";

export default function RetentionPage() {
  const { data: policies, isLoading } = useRetentionPolicies();
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [entityType, setEntityType] = useState("study");
  const [maxAge, setMaxAge] = useState(365);
  const [action, setAction] = useState("archive");

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.retention.create({ name, entity_type: entityType, max_age_days: maxAge, action });
    setShowForm(false);
    setName("");
    mutate("retention");
  };

  const handleDelete = async (id: string) => {
    if (confirm("Delete this policy?")) {
      await api.retention.delete(id);
      mutate("retention");
    }
  };

  const handleApply = async () => {
    if (confirm("Apply all retention policies now? This may delete data.")) {
      const result = await api.retention.apply();
      alert("Retention applied: " + JSON.stringify(result));
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Data Retention</h1>
        <div className="flex gap-2">
          <button onClick={handleApply} className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100">
            <Play className="w-4 h-4" /> Apply Now
          </button>
          <button onClick={() => setShowForm(!showForm)} className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700">
            <Plus className="w-4 h-4" /> Add Policy
          </button>
        </div>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 mb-4 flex flex-wrap gap-3 items-end">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} className="text-sm border border-gray-200 rounded-lg px-3 py-2" required />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Entity Type</label>
            <select value={entityType} onChange={(e) => setEntityType(e.target.value)} className="text-sm border border-gray-200 rounded-lg px-3 py-2">
              <option value="study">Study</option>
              <option value="job">Job</option>
              <option value="result">Result</option>
              <option value="audit">Audit Log</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Max Age (days)</label>
            <input type="number" value={maxAge} onChange={(e) => setMaxAge(Number(e.target.value))} className="text-sm border border-gray-200 rounded-lg px-3 py-2 w-24" min={1} />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Action</label>
            <select value={action} onChange={(e) => setAction(e.target.value)} className="text-sm border border-gray-200 rounded-lg px-3 py-2">
              <option value="archive">Archive</option>
              <option value="delete">Delete</option>
            </select>
          </div>
          <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700">Create</button>
        </form>
      )}

      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center text-gray-400">Loading...</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Name</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Entity Type</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Max Age</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Action</th>
                <th className="py-3 px-4"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(policies || []).map((p) => (
                <tr key={p.id} className="hover:bg-gray-50">
                  <td className="py-2.5 px-4 font-medium text-gray-900">{p.name}</td>
                  <td className="py-2.5 px-4"><span className="px-2 py-0.5 bg-blue-50 text-blue-700 text-xs rounded-full">{p.entity_type}</span></td>
                  <td className="py-2.5 px-4 text-gray-600">{p.max_age_days} days</td>
                  <td className="py-2.5 px-4"><span className={`px-2 py-0.5 text-xs rounded-full ${p.action === "delete" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>{p.action}</span></td>
                  <td className="py-2.5 px-4">
                    <button onClick={() => handleDelete(p.id)} className="text-red-500 hover:text-red-700"><Trash2 className="w-4 h-4" /></button>
                  </td>
                </tr>
              ))}
              {(!policies || policies.length === 0) && (
                <tr><td colSpan={5} className="py-12 text-center text-gray-400">No retention policies configured</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
