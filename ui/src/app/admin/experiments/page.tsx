"use client";

import { useState } from "react";
import { useExperiments } from "@/lib/hooks";
import { api } from "@/lib/api";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Table, Caption, Th } from "@/components/ui/Table";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import { Plus, Square, FlaskConical } from "lucide-react";
import { mutate } from "swr";

export default function ExperimentsPage() {
  const { data: experiments, isLoading } = useExperiments();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", usecase_name: "", control_version: "", treatment_version: "", traffic_split: 0.5 });
  const [pendingStopId, setPendingStopId] = useState<string | null>(null);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.experiments.create(form);
    setShowForm(false);
    setForm({ name: "", usecase_name: "", control_version: "", treatment_version: "", traffic_split: 0.5 });
    mutate("experiments");
  };

  const handleStop = async (id: string) => {
    await api.experiments.stop(id);
    mutate("experiments");
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">A/B Experiments</h1>
        <button onClick={() => setShowForm(!showForm)} className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700">
          <Plus className="w-4 h-4" /> New Experiment
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4 mb-4 grid grid-cols-2 gap-3">
          <div><label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Name</label><input value={form.name} onChange={(e) => setForm({...form, name: e.target.value})} className="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Use Case</label><input value={form.usecase_name} onChange={(e) => setForm({...form, usecase_name: e.target.value})} className="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Control Version</label><input value={form.control_version} onChange={(e) => setForm({...form, control_version: e.target.value})} className="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Treatment Version</label><input value={form.treatment_version} onChange={(e) => setForm({...form, treatment_version: e.target.value})} className="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2" required /></div>
          <div><label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Traffic Split</label><input type="number" step="0.05" min="0" max="1" value={form.traffic_split} onChange={(e) => setForm({...form, traffic_split: Number(e.target.value)})} className="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2" /></div>
          <div className="flex items-end"><button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700">Create</button></div>
        </form>
      )}

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <Table>
          <Caption>A/B experiments comparing model versions per use case</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Name</Th>
              <Th>Use Case</Th>
              <Th>Control</Th>
              <Th>Treatment</Th>
              <Th>Split</Th>
              <Th>Status</Th>
              <Th className="w-16" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <TableSkeleton rows={5} columnWidths={[120, 100, 90, 90, 60, 70, 32]} />
            ) : !experiments || experiments.length === 0 ? (
              <tr>
                <td colSpan={7}>
                  <EmptyState
                    icon={FlaskConical}
                    title="No experiments"
                    description="Create an A/B experiment to compare model versions on live traffic."
                  />
                </td>
              </tr>
            ) : (
              experiments.map((exp) => (
                <tr key={exp.id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-2.5 px-4 font-medium text-gray-900 dark:text-gray-100">{exp.name}</td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{exp.usecase_name}</td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{exp.control_version}</td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{exp.treatment_version}</td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{(exp.traffic_split * 100).toFixed(0)}%</td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${exp.is_active ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300" : "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"}`}>
                      {exp.is_active ? "Active" : "Stopped"}
                    </span>
                  </td>
                  <td className="py-2.5 px-4">
                    {exp.is_active && (
                      <button
                        onClick={() => setPendingStopId(exp.id)}
                        className="text-red-500 dark:text-red-400 hover:text-red-700"
                        aria-label={`Stop experiment ${exp.name}`}
                        title="Stop"
                      >
                        <Square className="w-4 h-4" />
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </Table>
      </div>
      <ConfirmDialog
        tier="modal"
        danger
        open={pendingStopId !== null}
        title="Stop Experiment"
        consequence="Traffic will immediately revert to the control version for this use case."
        confirmLabel="Stop Experiment"
        onConfirm={async () => {
          if (pendingStopId) await handleStop(pendingStopId);
          setPendingStopId(null);
        }}
        onCancel={() => setPendingStopId(null)}
      />
    </div>
  );
}
