"use client";

import { useState } from "react";
import Link from "next/link";
import { useTenants } from "@/lib/hooks";
import { api, CreatedTenant } from "@/lib/api";
import { Plus, Building2, CheckCircle } from "lucide-react";
import { mutate } from "swr";

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: "bg-green-50 text-green-700",
    suspended: "bg-amber-50 text-amber-700",
    offboarded: "bg-gray-100 text-gray-500",
  };
  return (
    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${styles[status] ?? "bg-gray-100 text-gray-500"}`}>
      {status}
    </span>
  );
}

export default function TenantsPage() {
  const { data: tenants, isLoading } = useTenants();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: "", slug: "", plan: "starter",
    admin_username: "", admin_email: "", admin_full_name: "",
  });
  const [created, setCreated] = useState<CreatedTenant | null>(null);
  const [error, setError] = useState("");

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const result = await api.tenants.create(form);
      setCreated(result);
      setShowForm(false);
      setForm({ name: "", slug: "", plan: "starter", admin_username: "", admin_email: "", admin_full_name: "" });
      mutate("tenants");
    } catch (e: any) {
      setError(e.message || "Failed to create tenant");
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Tenants</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700"
        >
          <Plus className="w-4 h-4" /> Create Tenant
        </button>
      </div>

      {created && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
          <div className="flex items-center gap-2 text-green-800 font-medium mb-2">
            <CheckCircle className="w-4 h-4" /> Tenant "{created.name}" created
          </div>
          <p className="text-sm text-green-900">
            Admin credentials — shown once, save them now:
          </p>
          <div className="mt-2 font-mono text-sm bg-white border border-green-200 rounded-lg p-3 inline-block">
            <div>username: {created.admin_username}</div>
            <div>password: {created.admin_temp_password}</div>
          </div>
          <div className="mt-3">
            <button onClick={() => setCreated(null)} className="text-xs text-green-700 hover:text-green-900 underline">
              Dismiss
            </button>
          </div>
        </div>
      )}

      {showForm && (
        <form onSubmit={handleCreate} className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 mb-4 space-y-3">
          {error && <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm">{error}</div>}
          <div className="flex flex-wrap gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Name</label>
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="text-sm border border-gray-200 rounded-lg px-3 py-2"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Slug</label>
              <input
                value={form.slug}
                onChange={(e) => setForm({ ...form, slug: e.target.value })}
                placeholder="hospital-a"
                pattern="[a-z0-9][a-z0-9-]*"
                className="text-sm border border-gray-200 rounded-lg px-3 py-2"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Plan</label>
              <input
                value={form.plan}
                onChange={(e) => setForm({ ...form, plan: e.target.value })}
                className="text-sm border border-gray-200 rounded-lg px-3 py-2 w-32"
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-3 items-end">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Admin Username</label>
              <input
                value={form.admin_username}
                onChange={(e) => setForm({ ...form, admin_username: e.target.value })}
                className="text-sm border border-gray-200 rounded-lg px-3 py-2"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Admin Email</label>
              <input
                type="email"
                value={form.admin_email}
                onChange={(e) => setForm({ ...form, admin_email: e.target.value })}
                className="text-sm border border-gray-200 rounded-lg px-3 py-2"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Admin Full Name</label>
              <input
                value={form.admin_full_name}
                onChange={(e) => setForm({ ...form, admin_full_name: e.target.value })}
                className="text-sm border border-gray-200 rounded-lg px-3 py-2"
              />
            </div>
            <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700">
              Create
            </button>
          </div>
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
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Slug</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Plan</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Status</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Features</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(tenants || []).map((t) => (
                <tr key={t.id} className="hover:bg-gray-50">
                  <td className="py-2.5 px-4 font-medium text-gray-900">
                    <Link href={`/admin/tenants/${t.id}`} className="flex items-center gap-2 hover:text-primary-600">
                      <Building2 className="w-4 h-4 text-gray-400" />
                      {t.name}
                    </Link>
                  </td>
                  <td className="py-2.5 px-4 text-gray-500 font-mono text-xs">{t.slug}</td>
                  <td className="py-2.5 px-4">
                    <span className="px-2 py-0.5 bg-blue-50 text-blue-700 text-xs rounded-full">{t.plan}</span>
                  </td>
                  <td className="py-2.5 px-4"><StatusBadge status={t.status} /></td>
                  <td className="py-2.5 px-4 text-gray-500">{t.features.length}</td>
                  <td className="py-2.5 px-4 text-gray-400 text-xs">
                    {t.created_at ? new Date(t.created_at).toLocaleDateString() : "-"}
                  </td>
                </tr>
              ))}
              {(!tenants || tenants.length === 0) && (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-gray-400">No tenants yet</td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
