"use client";

import { useState } from "react";
import Link from "next/link";
import { mutate } from "swr";
import { useTenants } from "@/lib/hooks";
import { api, CreatedTenant } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Modal } from "@/components/ui/Modal";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Table, Caption, Th } from "@/components/ui/Table";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { Building2, Plus, CheckCircle, ShieldAlert } from "lucide-react";

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300",
    suspended: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300",
    offboarded: "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400",
  };
  return (
    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${styles[status] ?? styles.offboarded}`}>
      {status}
    </span>
  );
}

const EMPTY_FORM = {
  name: "",
  slug: "",
  plan: "starter",
  admin_username: "",
  admin_email: "",
  admin_full_name: "",
};

export default function TenantsPage() {
  const { can } = useAuth();
  const { data: tenants, isLoading } = useTenants();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [created, setCreated] = useState<CreatedTenant | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!can("tenant.manage")) {
    return (
      <EmptyState
        icon={ShieldAlert}
        title="You don't have permission to manage tenants"
        description="This screen requires platform-admin access."
      />
    );
  }

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await api.tenants.create(form);
      setCreated(result);
      setShowForm(false);
      setForm(EMPTY_FORM);
      mutate("tenants");
    } catch (e: any) {
      setError(e.message || "Failed to create tenant");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Building2 className="w-7 h-7 text-primary-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Tenants</h1>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Plus className="w-4 h-4" /> Create Tenant
        </button>
      </div>

      {created && (
        <div className="bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg p-4 mb-4">
          <div className="flex items-center gap-2 text-green-800 dark:text-green-300 font-medium mb-2">
            <CheckCircle className="w-4 h-4" /> Tenant "{created.name}" created
          </div>
          <p className="text-sm text-green-900 dark:text-green-200">
            Admin credentials — shown once, save them now:
          </p>
          <div className="mt-2 font-mono text-sm bg-white dark:bg-surface border border-green-200 dark:border-green-900 rounded-lg p-3 inline-block">
            <div>username: {created.admin_username}</div>
            <div>password: {created.admin_temp_password}</div>
          </div>
          <div className="mt-3">
            <button
              onClick={() => setCreated(null)}
              className="text-xs text-green-700 dark:text-green-400 hover:text-green-900 dark:hover:text-green-200 underline"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <Table>
          <Caption>Tenant workspaces, their plan, status, and feature entitlements</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Name</Th>
              <Th>Slug</Th>
              <Th>Plan</Th>
              <Th>Status</Th>
              <Th>Features</Th>
              <Th>Created</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <TableSkeleton rows={4} columnWidths={[140, 100, 80, 70, 60, 90]} />
            ) : !tenants || tenants.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <EmptyState icon={Building2} title="No tenants yet" />
                </td>
              </tr>
            ) : (
              tenants.map((t) => (
                <tr key={t.id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-2.5 px-4 font-medium text-gray-900 dark:text-gray-100">
                    <Link href={`/admin/tenants/${t.id}`} className="flex items-center gap-2 hover:text-primary-600">
                      <Building2 className="w-4 h-4 text-gray-400" />
                      {t.name}
                    </Link>
                  </td>
                  <td className="py-2.5 px-4 text-gray-500 dark:text-gray-400 font-mono text-xs">{t.slug}</td>
                  <td className="py-2.5 px-4">
                    <span className="px-2 py-0.5 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 text-xs rounded-full">
                      {t.plan}
                    </span>
                  </td>
                  <td className="py-2.5 px-4"><StatusBadge status={t.status} /></td>
                  <td className="py-2.5 px-4 text-gray-500 dark:text-gray-400">{t.features.length}</td>
                  <td className="py-2.5 px-4 text-gray-400 dark:text-gray-500 text-xs">
                    {t.created_at ? new Date(t.created_at).toLocaleDateString() : "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </Table>
      </div>

      <Modal open={showForm} onClose={() => setShowForm(false)} title="Create Tenant" size="md">
        <form onSubmit={handleCreate} className="space-y-3">
          {error && <ErrorBanner message={error} className="mb-1" />}
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs">
              <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Name</span>
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
              />
            </label>
            <label className="block text-xs">
              <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Slug</span>
              <input
                required
                value={form.slug}
                onChange={(e) => setForm({ ...form, slug: e.target.value })}
                placeholder="hospital-a"
                pattern="[a-z0-9][a-z0-9-]*"
                className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
              />
            </label>
          </div>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Plan</span>
            <input
              value={form.plan}
              onChange={(e) => setForm({ ...form, plan: e.target.value })}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>
          <p className="text-xs text-gray-400 dark:text-gray-500 pt-2 border-t border-gray-100 dark:border-gray-800">
            The tenant's first admin user, provisioned in the same request:
          </p>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs">
              <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Admin Username</span>
              <input
                required
                value={form.admin_username}
                onChange={(e) => setForm({ ...form, admin_username: e.target.value })}
                className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
              />
            </label>
            <label className="block text-xs">
              <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Admin Email</span>
              <input
                required
                type="email"
                value={form.admin_email}
                onChange={(e) => setForm({ ...form, admin_email: e.target.value })}
                className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
              />
            </label>
          </div>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Admin Full Name</span>
            <input
              value={form.admin_full_name}
              onChange={(e) => setForm({ ...form, admin_full_name: e.target.value })}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setShowForm(false)}
              className="px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className="px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 rounded-lg transition-colors"
            >
              {busy ? "Creating…" : "Create Tenant"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
