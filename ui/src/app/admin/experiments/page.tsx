"use client";

import { useState } from "react";
import { useExperiments } from "@/lib/hooks";
import { api } from "@/lib/api";
import { Plus, Square, BarChart3 } from "lucide-react";
import { mutate } from "swr";

export default function ExperimentsPage() {
  const { data: experiments, isLoading } = useExperiments();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", usecase_name: "", control_version: "", treatment_version: "", traffic_split: 0.5 });

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.experiments.create(form);
    setShowForm(false);
    setForm({ name: "", usecase_name: "", control_version: "", treatment_version: "", traffic_split: 0.5 });
    mutate("experiments");
  };

  const handleStop = async (id: string) => {
    if (confirm("Stop this experiment?")) {
      await api.experiments.stop(id);
      mutate("experiments");
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">A/B Experiments</h1>
        <button onClick={() => setShowForm(!showForm)} className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700">
          <Plus className="w-4 h-4" /> New Experiment
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 mb-4 grid grid-cols-2 gap-3">
          <div><label className="block text-xs font-medium text-gray-500 mb-1">Name</label><input value={form.name} onChange={(e) => setForm({...form, name: e.target.value})} className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 mb-1">Use Case</label><input value={form.usecase_name} onChange={(e) => setForm({...form, usecase_name: e.target.value})} className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 mb-1">Control Version</label><input value={form.control_version} onChange={(e) => setForm({...form, control_version: e.target.value})} className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 mb-1">Treatment Version</label><input value={form.treatment_version} onChange={(e) => setForm({...form, treatment_version: e.target.value})} className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 mb-1">Traffic Split</label><input type="number" step="0.05" min="0" max="1" value={form.traffic_split} onChange={(e) => setForm({...form, traffic_split: Number(e.target.value)})} className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2" /></div>
          <div className="flex items-end"><button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700">Create</button></div>
        </form>
      )}

      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        {isLoading ? <div className="p-12 text-center text-gray-400">Loading...</div> : (
          <table className="w-full text-sm">
            <thead><tr className="border-b border-gray-100">
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Name</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Use Case</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Control</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Treatment</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Split</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Status</th>
              <th className="py-3 px-4"></th>
            </tr></thead>
            <tbody className="divide-y divide-gray-50">
              {(experiments || []).map((exp) => (
                <tr key={exp.id} className="hover:bg-gray-50">
                  <td className="py-2.5 px-4 font-medium text-gray-900">{exp.name}</td>
                  <td className="py-2.5 px-4 text-gray-600">{exp.usecase_name}</td>
                  <td className="py-2.5 px-4 text-gray-600">{exp.control_version}</td>
                  <td className="py-2.5 px-4 text-gray-600">{exp.treatment_version}</td>
                  <td className="py-2.5 px-4 text-gray-600">{(exp.traffic_split * 100).toFixed(0)}%</td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${exp.is_active ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-500"}`}>
                      {exp.is_active ? "Active" : "Stopped"}
                    </span>
                  </td>
                  <td className="py-2.5 px-4 flex gap-2">
                    {exp.is_active && (
                      <button onClick={() => handleStop(exp.id)} className="text-red-500 hover:text-red-700" title="Stop"><Square className="w-4 h-4" /></button>
                    )}
                  </td>
                </tr>
              ))}
              {(!experiments || experiments.length === 0) && (
                <tr><td colSpan={7} className="py-12 text-center text-gray-400">No experiments</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
