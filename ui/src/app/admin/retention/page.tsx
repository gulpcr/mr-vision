"use client";

import { useState } from "react";
import { useRetentionPolicies } from "@/lib/hooks";
import { api } from "@/lib/api";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Table, Caption, Th } from "@/components/ui/Table";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import { Plus, Trash2, Play, CheckCircle2, Database } from "lucide-react";
import { mutate } from "swr";

export default function RetentionPage() {
  const { data: policies, isLoading } = useRetentionPolicies();
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [entityType, setEntityType] = useState("study");
  const [maxAge, setMaxAge] = useState(365);
  const [action, setAction] = useState("archive");
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [pendingApply, setPendingApply] = useState(false);
  const [applyResult, setApplyResult] = useState<Record<string, any> | null>(null);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.retention.create({ name, entity_type: entityType, max_age_days: maxAge, action });
    setShowForm(false);
    setName("");
    mutate("retention");
  };

  const handleDelete = async (id: string) => {
    await api.retention.delete(id);
    mutate("retention");
  };

  const handleApply = async () => {
    const result = await api.retention.apply();
    setApplyResult(result as Record<string, any>);
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Data Retention</h1>
        <div className="flex gap-2">
          <button onClick={() => setPendingApply(true)} className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100">
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

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <Table>
          <Caption>Data retention policies by entity type</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Name</Th>
              <Th>Entity Type</Th>
              <Th>Max Age</Th>
              <Th>Action</Th>
              <Th className="w-16" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <TableSkeleton rows={4} columnWidths={[120, 90, 80, 80, 32]} />
            ) : !policies || policies.length === 0 ? (
              <tr>
                <td colSpan={5}>
                  <EmptyState
                    icon={Database}
                    title="No retention policies configured"
                    description="Add a policy to automatically archive or delete aged data."
                  />
                </td>
              </tr>
            ) : (
              policies.map((p) => (
                <tr key={p.id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-2.5 px-4 font-medium text-gray-900 dark:text-gray-100">{p.name}</td>
                  <td className="py-2.5 px-4"><span className="px-2 py-0.5 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 text-xs rounded-full">{p.entity_type}</span></td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{p.max_age_days} days</td>
                  <td className="py-2.5 px-4"><span className={`px-2 py-0.5 text-xs rounded-full ${p.action === "delete" ? "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300" : "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300"}`}>{p.action}</span></td>
                  <td className="py-2.5 px-4">
                    <button
                      onClick={() => setPendingDeleteId(p.id)}
                      aria-label={`Delete retention policy ${p.name}`}
                      className="text-red-500 hover:text-red-700"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </Table>
      </div>

      {applyResult && (
        <div className="mt-4 flex items-start gap-2 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg px-4 py-3 text-sm text-green-800 dark:text-green-300">
          <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
          <div>
            <p className="font-semibold">Retention applied.</p>
            <pre className="text-xs mt-1 font-mono whitespace-pre-wrap">{JSON.stringify(applyResult, null, 2)}</pre>
          </div>
        </div>
      )}

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingDeleteId !== null}
        title="Delete Retention Policy"
        consequence="This policy will no longer be applied to future retention runs. This cannot be undone."
        confirmLabel="Delete"
        onConfirm={async () => {
          if (pendingDeleteId) await handleDelete(pendingDeleteId);
          setPendingDeleteId(null);
        }}
        onCancel={() => setPendingDeleteId(null)}
      />
      <ConfirmDialog
        tier="modal"
        danger
        open={pendingApply}
        title="Apply Retention Policies Now"
        consequence="This runs every configured retention policy immediately and may permanently delete or archive data ahead of its normal schedule."
        confirmLabel="Apply Now"
        onConfirm={async () => {
          await handleApply();
          setPendingApply(false);
        }}
        onCancel={() => setPendingApply(false)}
      />
    </div>
  );
}
