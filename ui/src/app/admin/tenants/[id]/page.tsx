"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useTenant, useTenantApiKeys, useTenantUsers } from "@/lib/hooks";
import { api, TenantUser } from "@/lib/api";
import { startImpersonation } from "@/lib/impersonation";
import { mutate } from "swr";
import {
  ArrowLeft, Key, Trash2, Plus, Users, ShieldCheck, ShieldOff,
  UserCog, LogIn, X,
} from "lucide-react";

const API_KEY_SCOPES = ["dicom:upload"];

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

function PlanAndFeaturesCard({ tenantId }: { tenantId: string }) {
  const { data: tenant } = useTenant(tenantId);
  const [plan, setPlan] = useState("");
  const [newFeature, setNewFeature] = useState("");
  const [error, setError] = useState("");

  if (!tenant) return null;
  const currentPlan = plan || tenant.plan;

  const savePlan = async () => {
    setError("");
    try {
      await api.tenants.updatePlan(tenantId, currentPlan);
      mutate(["tenant", tenantId]);
      mutate("tenants");
    } catch (e: any) {
      setError(e.message || "Failed to update plan");
    }
  };

  const addFeature = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newFeature.trim()) return;
    setError("");
    try {
      await api.tenants.updateFeatures(tenantId, [...tenant.features, newFeature.trim()]);
      setNewFeature("");
      mutate(["tenant", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update features");
    }
  };

  const removeFeature = async (feature: string) => {
    setError("");
    try {
      await api.tenants.updateFeatures(tenantId, tenant.features.filter((f) => f !== feature));
      mutate(["tenant", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update features");
    }
  };

  const updateStatus = async (status: string) => {
    if (!confirm(`Set this tenant's status to "${status}"?`)) return;
    setError("");
    try {
      await api.tenants.updateStatus(tenantId, status);
      mutate(["tenant", tenantId]);
      mutate("tenants");
    } catch (e: any) {
      setError(e.message || "Failed to update status");
    }
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 mb-6">
      <div className="px-5 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-gray-900">Plan &amp; Features</h2>
      </div>
      <div className="p-5 space-y-4">
        {error && <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm">{error}</div>}

        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-600 font-medium w-20">Plan</label>
          <input
            defaultValue={tenant.plan}
            onChange={(e) => setPlan(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 w-40"
          />
          <button onClick={savePlan} className="px-3 py-1.5 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700">
            Save
          </button>
        </div>

        <div>
          <label className="text-sm text-gray-600 font-medium block mb-2">Features</label>
          <div className="flex flex-wrap gap-2 mb-2">
            {tenant.features.length === 0 && <span className="text-sm text-gray-400">None</span>}
            {tenant.features.map((f) => (
              <span key={f} className="flex items-center gap-1 px-2 py-1 bg-blue-50 text-blue-700 text-xs rounded-full">
                {f}
                <button onClick={() => removeFeature(f)} className="hover:text-blue-900">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
          </div>
          <form onSubmit={addFeature} className="flex gap-2">
            <input
              value={newFeature}
              onChange={(e) => setNewFeature(e.target.value)}
              placeholder="e.g. cds, longitudinal"
              className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 w-56"
            />
            <button type="submit" className="px-3 py-1.5 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700">
              Add
            </button>
          </form>
        </div>

        <div>
          <label className="text-sm text-gray-600 font-medium block mb-2">Status</label>
          <div className="flex items-center gap-2">
            <StatusBadge status={tenant.status} />
            <div className="flex gap-2 ml-2">
              {tenant.status !== "active" && (
                <button onClick={() => updateStatus("active")} className="px-3 py-1 text-xs font-medium text-green-700 bg-green-50 border border-green-200 rounded-lg hover:bg-green-100">
                  Reactivate
                </button>
              )}
              {tenant.status !== "suspended" && (
                <button onClick={() => updateStatus("suspended")} className="px-3 py-1 text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100">
                  Suspend
                </button>
              )}
              {tenant.status !== "offboarded" && (
                <button onClick={() => updateStatus("offboarded")} className="px-3 py-1 text-xs font-medium text-red-700 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100">
                  Offboard
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function ApiKeysCard({ tenantId }: { tenantId: string }) {
  const { data: keys, isLoading } = useTenantApiKeys(tenantId);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [error, setError] = useState("");

  const toggleScope = (scope: string) => {
    setScopes((prev) => (prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope]));
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const result = await api.tenantApiKeys.create(tenantId, { name, scopes });
      setRevealedKey(result.key);
      setShowForm(false);
      setName("");
      setScopes([]);
      mutate(["tenant-api-keys", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to create API key");
    }
  };

  const handleRevoke = async (keyId: string) => {
    if (!confirm("Revoke this API key? Anything using it will stop working immediately.")) return;
    await api.tenantApiKeys.revoke(tenantId, keyId);
    mutate(["tenant-api-keys", tenantId]);
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 mb-6">
      <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
        <div>
          <h2 className="font-semibold text-gray-900">DICOM Upload API Keys</h2>
          <p className="text-xs text-gray-500 mt-0.5">Scoped, revocable keys for this tenant's external systems</p>
        </div>
        <button onClick={() => setShowForm(!showForm)} className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700">
          <Plus className="w-4 h-4" /> New Key
        </button>
      </div>
      <div className="p-5">
        {error && <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm mb-3">{error}</div>}

        {revealedKey && (
          <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
            <p className="text-sm text-green-800 mb-2">Key created — shown once, save it now:</p>
            <div className="font-mono text-sm bg-white border border-green-200 rounded-lg p-3 break-all">{revealedKey}</div>
            <button onClick={() => setRevealedKey(null)} className="mt-2 text-xs text-green-700 hover:text-green-900 underline">
              Dismiss
            </button>
          </div>
        )}

        {showForm && (
          <form onSubmit={handleCreate} className="border border-gray-200 rounded-lg p-4 mb-4 space-y-3">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} className="text-sm border border-gray-200 rounded-lg px-3 py-2 w-64" required />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Scopes</label>
              <div className="flex gap-3">
                {API_KEY_SCOPES.map((scope) => (
                  <label key={scope} className="flex items-center gap-1.5 text-sm text-gray-700">
                    <input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggleScope(scope)} />
                    {scope}
                  </label>
                ))}
              </div>
            </div>
            <button type="submit" disabled={scopes.length === 0} className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700 disabled:opacity-50">
              Create
            </button>
          </form>
        )}

        {isLoading ? (
          <div className="text-center text-gray-400 py-8">Loading...</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Name</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Prefix</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Scopes</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Status</th>
                <th className="py-2 px-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(keys || []).map((k) => (
                <tr key={k.id}>
                  <td className="py-2 px-2 font-medium text-gray-900 flex items-center gap-2"><Key className="w-3.5 h-3.5 text-gray-400" />{k.name}</td>
                  <td className="py-2 px-2 font-mono text-xs text-gray-500">{k.prefix}...</td>
                  <td className="py-2 px-2 text-xs text-gray-500">{k.scopes.join(", ")}</td>
                  <td className="py-2 px-2">
                    <span className={`px-2 py-0.5 text-xs rounded-full ${k.is_active ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-500"}`}>
                      {k.is_active ? "Active" : "Revoked"}
                    </span>
                  </td>
                  <td className="py-2 px-2">
                    {k.is_active && (
                      <button onClick={() => handleRevoke(k.id)} className="text-red-500 hover:text-red-700">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {(!keys || keys.length === 0) && (
                <tr><td colSpan={5} className="py-8 text-center text-gray-400">No API keys yet</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function UsersCard({ tenantId }: { tenantId: string }) {
  const { data: users, isLoading } = useTenantUsers(tenantId);
  const router = useRouter();
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const toggleAdmin = async (user: TenantUser) => {
    setBusyUserId(user.id);
    setError("");
    try {
      await api.auth.updatePlatformAdmin(user.id, !user.is_platform_admin);
      mutate(["tenant-users", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update platform-admin status");
    } finally {
      setBusyUserId(null);
    }
  };

  const toggleOperator = async (user: TenantUser) => {
    setBusyUserId(user.id);
    setError("");
    try {
      await api.auth.updatePlatformOperator(user.id, !user.is_platform_operator);
      mutate(["tenant-users", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update platform-operator status");
    } finally {
      setBusyUserId(null);
    }
  };

  const impersonate = async (user: TenantUser) => {
    if (!confirm(`Impersonate ${user.username}? You'll see the app exactly as they do for 30 minutes.`)) return;
    setBusyUserId(user.id);
    setError("");
    try {
      await startImpersonation(user.id);
      router.push("/dashboard");
    } catch (e: any) {
      setError(e.message || "Failed to start impersonation");
      setBusyUserId(null);
    }
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200">
      <div className="px-5 py-4 border-b border-gray-100 flex items-center gap-2">
        <Users className="w-4 h-4 text-gray-600" />
        <h2 className="font-semibold text-gray-900">Users</h2>
      </div>
      <div className="p-5">
        {error && <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm mb-3">{error}</div>}
        {isLoading ? (
          <div className="text-center text-gray-400 py-8">Loading...</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Username</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Role</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">MFA</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Platform Admin</th>
                <th className="text-left py-2 px-2 text-xs font-semibold text-gray-500 uppercase">Operator</th>
                <th className="py-2 px-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(users || []).map((u) => (
                <tr key={u.id}>
                  <td className="py-2 px-2 font-medium text-gray-900">{u.username}</td>
                  <td className="py-2 px-2 text-gray-500">{u.role}</td>
                  <td className="py-2 px-2">
                    {u.totp_enabled ? (
                      <span className="px-2 py-0.5 bg-green-50 text-green-700 text-xs rounded-full">Enabled</span>
                    ) : (
                      <span className="px-2 py-0.5 bg-gray-100 text-gray-500 text-xs rounded-full">Off</span>
                    )}
                  </td>
                  <td className="py-2 px-2">
                    <button
                      onClick={() => toggleAdmin(u)}
                      disabled={busyUserId === u.id}
                      className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full disabled:opacity-50 ${
                        u.is_platform_admin ? "bg-purple-50 text-purple-700" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                      }`}
                    >
                      {u.is_platform_admin ? <ShieldCheck className="w-3 h-3" /> : <ShieldOff className="w-3 h-3" />}
                      {u.is_platform_admin ? "Admin" : "Grant"}
                    </button>
                  </td>
                  <td className="py-2 px-2">
                    <button
                      onClick={() => toggleOperator(u)}
                      disabled={busyUserId === u.id}
                      className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full disabled:opacity-50 ${
                        u.is_platform_operator ? "bg-indigo-50 text-indigo-700" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                      }`}
                    >
                      <UserCog className="w-3 h-3" />
                      {u.is_platform_operator ? "Operator" : "Grant"}
                    </button>
                  </td>
                  <td className="py-2 px-2">
                    <button
                      onClick={() => impersonate(u)}
                      disabled={busyUserId === u.id}
                      className="flex items-center gap-1 px-2 py-1 text-xs font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50"
                    >
                      <LogIn className="w-3 h-3" /> Impersonate
                    </button>
                  </td>
                </tr>
              ))}
              {(!users || users.length === 0) && (
                <tr><td colSpan={6} className="py-8 text-center text-gray-400">No users in this tenant</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default function TenantDetailPage() {
  const params = useParams();
  const tenantId = params.id as string;
  const { data: tenant } = useTenant(tenantId);

  return (
    <div>
      <Link href="/admin/tenants" className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 mb-4">
        <ArrowLeft className="w-4 h-4" /> Back to Tenants
      </Link>

      <div className="flex items-center gap-3 mb-6">
        <h1 className="text-2xl font-bold text-gray-900">{tenant?.name ?? "Loading..."}</h1>
        {tenant && <StatusBadge status={tenant.status} />}
        {tenant && <span className="text-sm text-gray-400 font-mono">{tenant.slug}</span>}
      </div>

      <PlanAndFeaturesCard tenantId={tenantId} />
      <ApiKeysCard tenantId={tenantId} />
      <UsersCard tenantId={tenantId} />
    </div>
  );
}
