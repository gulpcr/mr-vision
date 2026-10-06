"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { mutate } from "swr";
import { useTenant, useTenantApiKeys, useTenantUsers } from "@/lib/hooks";
import { api, TenantUser, type DicomEndpoint, type Plan, type TenantBAA } from "@/lib/api";
import { startImpersonation } from "@/lib/impersonation";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Table, Caption, Th } from "@/components/ui/Table";
import {
  ArrowLeft, Key, Trash2, Plus, Users, ShieldCheck, ShieldOff,
  UserCog, LogIn, X,
} from "lucide-react";

const API_KEY_SCOPES = ["dicom:upload"];

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

function PlanAndFeaturesCard({ tenantId }: { tenantId: string }) {
  const { data: tenant } = useTenant(tenantId);
  const [plan, setPlan] = useState("");
  const [plans, setPlans] = useState<Plan[]>([]);
  useEffect(() => {
    api.plans.list().then(setPlans).catch(() => setPlans([]));
  }, []);
  const [newFeature, setNewFeature] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pendingStatus, setPendingStatus] = useState<string | null>(null);

  if (!tenant) return null;
  const currentPlan = plan || tenant.plan;

  const savePlan = async () => {
    setError(null);
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
    setError(null);
    try {
      await api.tenants.updateFeatures(tenantId, [...tenant.features, newFeature.trim()]);
      setNewFeature("");
      mutate(["tenant", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update features");
    }
  };

  const removeFeature = async (feature: string) => {
    setError(null);
    try {
      await api.tenants.updateFeatures(tenantId, tenant.features.filter((f) => f !== feature));
      mutate(["tenant", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update features");
    }
  };

  const confirmStatusChange = async () => {
    if (!pendingStatus) return;
    setError(null);
    try {
      await api.tenants.updateStatus(tenantId, pendingStatus);
      mutate(["tenant", tenantId]);
      mutate("tenants");
    } catch (e: any) {
      setError(e.message || "Failed to update status");
    }
    setPendingStatus(null);
  };

  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 mb-6">
      <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100">Plan &amp; Features</h2>
      </div>
      <div className="p-5 space-y-4">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-600 dark:text-gray-400 font-medium w-20">Plan</label>
          <select
            value={currentPlan}
            onChange={(e) => setPlan(e.target.value)}
            className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5 w-56"
          >
            {!plans.some((p) => p.name === currentPlan) && <option value={currentPlan}>{currentPlan}</option>}
            {plans.filter((p) => p.is_active || p.name === currentPlan).map((p) => (
              <option key={p.name} value={p.name}>{p.display_name}</option>
            ))}
          </select>
          <button
            onClick={savePlan}
            className="px-3 py-1.5 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
          >
            Save
          </button>
        </div>

        {(() => {
          const p = plans.find((x) => x.name === currentPlan);
          if (!p) return null;
          return (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {p.usecases === null ? "All AI use cases" : `${p.usecases.length} AI use case(s)`}
              {" · "}
              {p.permissions.includes("*") ? "no permission ceiling" : `${p.permissions.length} permission(s) allowed`}
              {p.default_max_users ? ` · default ${p.default_max_users} seats` : ""}
            </p>
          );
        })()}

        <div>
          <label className="text-sm text-gray-600 dark:text-gray-400 font-medium block mb-2">
            Extra use cases <span className="font-normal text-gray-400">(added on top of the plan)</span>
          </label>
          <div className="flex flex-wrap gap-2 mb-2">
            {tenant.features.length === 0 && <span className="text-sm text-gray-400 dark:text-gray-500">None</span>}
            {tenant.features.map((f) => (
              <span
                key={f}
                className="flex items-center gap-1 px-2 py-1 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 text-xs rounded-full"
              >
                {f}
                <button onClick={() => removeFeature(f)} className="hover:text-blue-900 dark:hover:text-blue-100">
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
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5 w-56"
            />
            <button
              type="submit"
              className="px-3 py-1.5 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700 transition-colors"
            >
              Add
            </button>
          </form>
        </div>

        <div>
          <label className="text-sm text-gray-600 dark:text-gray-400 font-medium block mb-2">Status</label>
          <div className="flex items-center gap-2">
            <StatusBadge status={tenant.status} />
            <div className="flex gap-2 ml-2">
              {tenant.status !== "active" && (
                <button
                  onClick={() => setPendingStatus("active")}
                  className="px-3 py-1 text-xs font-medium text-green-700 dark:text-green-300 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg hover:bg-green-100 dark:hover:bg-green-900 transition-colors"
                >
                  Reactivate
                </button>
              )}
              {tenant.status !== "suspended" && (
                <button
                  onClick={() => setPendingStatus("suspended")}
                  className="px-3 py-1 text-xs font-medium text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-900 rounded-lg hover:bg-amber-100 dark:hover:bg-amber-900 transition-colors"
                >
                  Suspend
                </button>
              )}
              {tenant.status !== "offboarded" && (
                <button
                  onClick={() => setPendingStatus("offboarded")}
                  className="px-3 py-1 text-xs font-medium text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg hover:bg-red-100 dark:hover:bg-red-900 transition-colors"
                >
                  Offboard
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        tier="modal"
        danger={pendingStatus === "offboarded" || pendingStatus === "suspended"}
        open={pendingStatus !== null}
        title="Change Tenant Status"
        consequence={
          pendingStatus
            ? `Set this tenant's status to "${pendingStatus}"? ${
                pendingStatus === "suspended"
                  ? "Every user in this tenant will immediately lose access."
                  : pendingStatus === "offboarded"
                  ? "This tenant will be permanently inaccessible."
                  : "Users regain access immediately."
              }`
            : ""
        }
        confirmLabel="Confirm"
        onConfirm={confirmStatusChange}
        onCancel={() => setPendingStatus(null)}
      />
    </div>
  );
}

function ApiKeysCard({ tenantId }: { tenantId: string }) {
  const { data: keys, isLoading } = useTenantApiKeys(tenantId);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<{ id: string; name: string } | null>(null);

  const toggleScope = (scope: string) => {
    setScopes((prev) => (prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope]));
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
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

  const confirmRevoke = async () => {
    if (!pendingRevoke) return;
    try {
      await api.tenantApiKeys.revoke(tenantId, pendingRevoke.id);
      mutate(["tenant-api-keys", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to revoke key");
    }
    setPendingRevoke(null);
  };

  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 mb-6">
      <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
        <div>
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">DICOM Upload API Keys</h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Scoped, revocable keys for this tenant's external systems
          </p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Plus className="w-4 h-4" /> New Key
        </button>
      </div>
      <div className="p-5">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-3" />}

        {revealedKey && (
          <div className="bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg p-4 mb-4">
            <p className="text-sm text-green-800 dark:text-green-300 mb-2">Key created — shown once, save it now:</p>
            <div className="font-mono text-sm bg-white dark:bg-surface border border-green-200 dark:border-green-900 rounded-lg p-3 break-all">
              {revealedKey}
            </div>
            <button
              onClick={() => setRevealedKey(null)}
              className="mt-2 text-xs text-green-700 dark:text-green-400 hover:text-green-900 dark:hover:text-green-200 underline"
            >
              Dismiss
            </button>
          </div>
        )}

        {showForm && (
          <form
            onSubmit={handleCreate}
            className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 mb-4 space-y-3"
          >
            <div>
              <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2 w-64"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Scopes</label>
              <div className="flex gap-3">
                {API_KEY_SCOPES.map((scope) => (
                  <label key={scope} className="flex items-center gap-1.5 text-sm text-gray-700 dark:text-gray-300">
                    <input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggleScope(scope)} />
                    {scope}
                  </label>
                ))}
              </div>
            </div>
            <button
              type="submit"
              disabled={scopes.length === 0}
              className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors"
            >
              Create
            </button>
          </form>
        )}

        <Table>
          <Caption>DICOM upload API keys for this tenant</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Name</Th>
              <Th>Prefix</Th>
              <Th>Scopes</Th>
              <Th>Status</Th>
              <Th className="w-10" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <tr>
                <td colSpan={5} className="py-8 text-center text-gray-400 dark:text-gray-500">Loading...</td>
              </tr>
            ) : !keys || keys.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-8 text-center text-gray-400 dark:text-gray-500">No API keys yet</td>
              </tr>
            ) : (
              keys.map((k) => (
                <tr key={k.id}>
                  <td className="py-2 px-2 font-medium text-gray-900 dark:text-gray-100 flex items-center gap-2">
                    <Key className="w-3.5 h-3.5 text-gray-400" />{k.name}
                  </td>
                  <td className="py-2 px-2 font-mono text-xs text-gray-500 dark:text-gray-400">{k.prefix}...</td>
                  <td className="py-2 px-2 text-xs text-gray-500 dark:text-gray-400">{k.scopes.join(", ")}</td>
                  <td className="py-2 px-2">
                    <span
                      className={`px-2 py-0.5 text-xs rounded-full ${
                        k.is_active
                          ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300"
                          : "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
                      }`}
                    >
                      {k.is_active ? "Active" : "Revoked"}
                    </span>
                  </td>
                  <td className="py-2 px-2">
                    {k.is_active && (
                      <button
                        onClick={() => setPendingRevoke({ id: k.id, name: k.name })}
                        aria-label={`Revoke ${k.name}`}
                        className="text-red-500 hover:text-red-700 dark:hover:text-red-400"
                      >
                        <Trash2 className="w-4 h-4" />
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
        open={pendingRevoke !== null}
        title="Revoke API Key"
        consequence={
          pendingRevoke
            ? `Revoke "${pendingRevoke.name}"? Anything using it will stop working immediately.`
            : ""
        }
        confirmLabel="Revoke"
        onConfirm={confirmRevoke}
        onCancel={() => setPendingRevoke(null)}
      />
    </div>
  );
}

function UsersCard({ tenantId }: { tenantId: string }) {
  const { data: users, isLoading } = useTenantUsers(tenantId);
  const router = useRouter();
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingImpersonate, setPendingImpersonate] = useState<TenantUser | null>(null);

  const toggleAdmin = async (user: TenantUser) => {
    setBusyUserId(user.id);
    setError(null);
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
    setError(null);
    try {
      await api.auth.updatePlatformOperator(user.id, !user.is_platform_operator);
      mutate(["tenant-users", tenantId]);
    } catch (e: any) {
      setError(e.message || "Failed to update platform-operator status");
    } finally {
      setBusyUserId(null);
    }
  };

  const confirmImpersonate = async () => {
    if (!pendingImpersonate) return;
    setBusyUserId(pendingImpersonate.id);
    setError(null);
    try {
      await startImpersonation(pendingImpersonate.id);
      router.push("/dashboard");
    } catch (e: any) {
      setError(e.message || "Failed to start impersonation");
      setBusyUserId(null);
    }
    setPendingImpersonate(null);
  };

  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
      <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2">
        <Users className="w-4 h-4 text-gray-600 dark:text-gray-400" />
        <h2 className="font-semibold text-gray-900 dark:text-gray-100">Users</h2>
      </div>
      <div className="p-5">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-3" />}

        <Table>
          <Caption>Users belonging to this tenant, and their platform-level access</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Username</Th>
              <Th>Role</Th>
              <Th>MFA</Th>
              <Th>Platform Admin</Th>
              <Th>Operator</Th>
              <Th className="w-24" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <tr>
                <td colSpan={6} className="py-8 text-center text-gray-400 dark:text-gray-500">Loading...</td>
              </tr>
            ) : !users || users.length === 0 ? (
              <tr>
                <td colSpan={6} className="py-8 text-center text-gray-400 dark:text-gray-500">No users in this tenant</td>
              </tr>
            ) : (
              users.map((u) => (
                <tr key={u.id}>
                  <td className="py-2 px-2 font-medium text-gray-900 dark:text-gray-100">{u.username}</td>
                  <td className="py-2 px-2 text-gray-500 dark:text-gray-400">{u.role}</td>
                  <td className="py-2 px-2">
                    {u.totp_enabled ? (
                      <span className="px-2 py-0.5 bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300 text-xs rounded-full">
                        Enabled
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-xs rounded-full">
                        Off
                      </span>
                    )}
                  </td>
                  <td className="py-2 px-2">
                    <button
                      onClick={() => toggleAdmin(u)}
                      disabled={busyUserId === u.id}
                      className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full disabled:opacity-50 transition-colors ${
                        u.is_platform_admin
                          ? "bg-purple-50 dark:bg-purple-950 text-purple-700 dark:text-purple-300"
                          : "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700"
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
                      className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full disabled:opacity-50 transition-colors ${
                        u.is_platform_operator
                          ? "bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300"
                          : "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700"
                      }`}
                    >
                      <UserCog className="w-3 h-3" />
                      {u.is_platform_operator ? "Operator" : "Grant"}
                    </button>
                  </td>
                  <td className="py-2 px-2">
                    <button
                      onClick={() => setPendingImpersonate(u)}
                      disabled={busyUserId === u.id}
                      className="flex items-center gap-1 px-2 py-1 text-xs font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors"
                    >
                      <LogIn className="w-3 h-3" /> Impersonate
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </Table>
      </div>

      <ConfirmDialog
        tier="modal"
        open={pendingImpersonate !== null}
        title="Impersonate User"
        consequence={
          pendingImpersonate
            ? `Impersonate ${pendingImpersonate.username}? You'll see the app exactly as they do for 30 minutes.`
            : ""
        }
        confirmLabel="Impersonate"
        onConfirm={confirmImpersonate}
        onCancel={() => setPendingImpersonate(null)}
      />
    </div>
  );
}

function DicomEndpointsCard({ tenantId }: { tenantId: string }) {
  const [rows, setRows] = useState<DicomEndpoint[]>([]);
  const [called, setCalled] = useState("");
  const [calling, setCalling] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const load = () => api.tenants.listDicomEndpoints(tenantId).then(setRows).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [tenantId]); // eslint-disable-line react-hooks/exhaustive-deps

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      await api.tenants.createDicomEndpoint(tenantId, { called_aet: called, calling_aet: calling || undefined });
      setCalled(""); setCalling("");
      load();
    } catch (e: any) { setErr(e.message); }
  };

  return (
    <section className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 mb-6">
      <h2 className="font-semibold text-gray-900 dark:text-gray-100">DICOM AE titles</h2>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Studies that scanners send (C-STORE) to one of these called AE titles are attributed to this tenant.
        Leave &quot;calling AE&quot; empty to accept any scanner.
      </p>
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      <ul className="divide-y divide-gray-100 dark:divide-gray-800 text-sm mb-3">
        {rows.length === 0 && <li className="py-2 text-gray-400">No AE titles — scanner studies can&apos;t reach this tenant yet.</li>}
        {rows.map((r) => (
          <li key={r.id} className="py-2 flex items-center justify-between">
            <span className="font-mono">{r.called_aet}{r.calling_aet ? ` ← ${r.calling_aet}` : " ← any scanner"}</span>
            <button onClick={async () => { await api.tenants.deleteDicomEndpoint(tenantId, r.id).catch((e) => setErr(e.message)); load(); }}
              className="text-xs text-red-600 hover:underline">Remove</button>
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="flex flex-wrap gap-2">
        <input required maxLength={16} value={called} onChange={(e) => setCalled(e.target.value.toUpperCase())}
          placeholder="Called AE (e.g. CITYHOSP_AI)"
          className="text-sm font-mono border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5" />
        <input maxLength={16} value={calling} onChange={(e) => setCalling(e.target.value.toUpperCase())}
          placeholder="Calling AE (optional)"
          className="text-sm font-mono border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5" />
        <button type="submit" className="px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700">Add</button>
      </form>
    </section>
  );
}

function LimitsAndPurgeCard({ tenantId, slug, maxUsersNow }: { tenantId: string; slug: string; maxUsersNow: number | null }) {
  const [maxUsers, setMaxUsers] = useState(maxUsersNow ? String(maxUsersNow) : "");
  useEffect(() => setMaxUsers(maxUsersNow ? String(maxUsersNow) : ""), [maxUsersNow]);
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const protectedTenant = tenantId === "default";

  return (
    <section className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 mb-6">
      <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-3">Limits &amp; lifecycle</h2>
      {msg && <p className="text-sm text-green-700 dark:text-green-300 mb-2">{msg}</p>}
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      <div className="flex flex-wrap items-end gap-2 mb-5">
        <label className="text-sm">
          <span className="block text-xs text-gray-500 dark:text-gray-400">Seat limit (blank = unlimited)</span>
          <input type="number" min={1} value={maxUsers} onChange={(e) => setMaxUsers(e.target.value)}
            className="mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5 w-40" />
        </label>
        <button
          onClick={async () => {
            setErr(null);
            try {
              const r = await api.platform.updateLimits(tenantId, maxUsers ? Number(maxUsers) : null);
              setMsg(`Seat limit: ${r.max_users ?? "unlimited"}`);
              mutate(["tenant", tenantId]);
            } catch (e: any) { setErr(e.message); }
          }}
          className="px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700">
          Save limit
        </button>
      </div>
      {!protectedTenant && (
        <div className="border-t border-red-100 dark:border-red-900/40 pt-4">
          <p className="text-sm font-medium text-red-700 dark:text-red-400">Purge tenant</p>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
            Permanently deletes every study, result, report, patient, user and setting of this tenant, its stored
            artifacts and its PACS studies. The audit log is kept. Type the slug <span className="font-mono">{slug}</span> to confirm.
          </p>
          <div className="flex gap-2">
            <input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={slug}
              className="text-sm font-mono border border-red-200 dark:border-red-900 dark:bg-surface-raised rounded-lg px-3 py-1.5" />
            <button
              disabled={confirm !== slug}
              onClick={async () => {
                setErr(null);
                try {
                  const r = await api.platform.purgeTenant(tenantId, confirm);
                  setMsg(`Purged: ${Object.values(r.rows_deleted ?? {}).reduce((a: number, b: any) => a + Number(b), 0)} rows, ${r.orthanc_studies_deleted} PACS studies.`);
                } catch (e: any) { setErr(e.message); }
              }}
              className="px-3 py-1.5 text-sm font-medium rounded-lg bg-red-600 text-white disabled:opacity-40">
              Purge
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** ADM-01: the business associate agreement on file. Only the signed document's SHA-256 is
 * sent (computed here, in the browser); the document stays in the compliance repository. */
function BaaCard({ tenantId }: { tenantId: string }) {
  const today = new Date().toISOString().slice(0, 10);
  const [rows, setRows] = useState<TenantBAA[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState({ counterparty: "", signatory_name: "", signatory_title: "",
    signed_on: today, effective_from: today, expires_on: "" });
  const [doc, setDoc] = useState<{ name: string; sha256: string } | null>(null);
  const load = () => api.tenants.listBaas(tenantId).then(setRows).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [tenantId]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.value });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!doc) return;
    setErr(null);
    try {
      await api.tenants.recordBaa(tenantId, {
        ...form, signatory_title: form.signatory_title || null, expires_on: form.expires_on || null,
        document_name: doc.name, document_sha256: doc.sha256,
      });
      setDoc(null);
      load();
    } catch (e: any) { setErr(e.message); }
  };
  const inp = "text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5";
  const active = rows.some((r) => r.active);

  return (
    <section className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 mb-6">
      <h2 className="font-semibold text-gray-900 dark:text-gray-100">Business associate agreement</h2>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
        No patient data may reach this workspace before a BAA is signed. While required, the workspace
        cannot be activated or given AE titles / API keys without an active BAA on file.
      </p>
      {!active && <p className="text-sm text-amber-700 dark:text-amber-400 mb-2">No active BAA on file.</p>}
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      <ul className="divide-y divide-gray-100 dark:divide-gray-800 text-sm mb-3">
        {rows.map((r) => (
          <li key={r.id} className="py-2 flex items-start justify-between gap-3">
            <div>
              <div className="font-medium">{r.counterparty} · {r.signatory_name}{r.signatory_title ? `, ${r.signatory_title}` : ""}</div>
              <div className="text-xs text-gray-500">
                Signed {r.signed_on} · effective {r.effective_from}{r.expires_on ? ` → ${r.expires_on}` : ""}
                {r.terminated_at ? ` · terminated ${new Date(r.terminated_at).toLocaleDateString()}` : ""}
              </div>
              <div className="text-xs font-mono text-gray-400 break-all">{r.document_name} · {r.document_sha256}</div>
            </div>
            {r.active ? (
              <button onClick={async () => {
                if (!confirm("Mark this BAA terminated? New AE titles and API keys will be refused.")) return;
                await api.tenants.terminateBaa(tenantId, r.id).catch((e) => setErr(e.message)); load();
              }} className="text-xs text-red-600 hover:underline whitespace-nowrap">Terminate</button>
            ) : <span className="text-xs text-gray-400">inactive</span>}
          </li>
        ))}
      </ul>
      <form onSubmit={save} className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <input required value={form.counterparty} onChange={set("counterparty")} placeholder="Customer legal name" className={inp} />
        <input required value={form.signatory_name} onChange={set("signatory_name")} placeholder="Signatory name" className={inp} />
        <input value={form.signatory_title} onChange={set("signatory_title")} placeholder="Signatory title" className={inp} />
        <label className="text-xs text-gray-500">Signed on <input type="date" required max={today} value={form.signed_on} onChange={set("signed_on")} className={inp} /></label>
        <label className="text-xs text-gray-500">Effective from <input type="date" required value={form.effective_from} onChange={set("effective_from")} className={inp} /></label>
        <label className="text-xs text-gray-500">Expires (optional) <input type="date" value={form.expires_on} onChange={set("expires_on")} className={inp} /></label>
        <label className="text-xs text-gray-500 sm:col-span-2">Signed BAA (PDF; only its SHA-256 is uploaded)
          <input type="file" required accept=".pdf,application/pdf" className="block text-sm mt-1"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              setDoc(f ? { name: f.name, sha256: await sha256Hex(f) } : null);
            }} />
        </label>
        {doc && <p className="text-xs font-mono text-gray-500 sm:col-span-2 break-all">SHA-256 {doc.sha256}</p>}
        <div className="sm:col-span-2">
          <button type="submit" disabled={!doc} className="px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50">Record BAA</button>
        </div>
      </form>
    </section>
  );
}

export default function TenantDetailPage() {
  const params = useParams();
  const tenantId = params.id as string;
  const { data: tenant } = useTenant(tenantId);

  return (
    <div>
      <Link
        href="/admin/tenants"
        className="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 mb-4"
      >
        <ArrowLeft className="w-4 h-4" /> Back to Tenants
      </Link>

      <div className="flex items-center gap-3 mb-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">{tenant?.name ?? "Loading..."}</h1>
        {tenant && <StatusBadge status={tenant.status} />}
        {tenant && <span className="text-sm text-gray-400 dark:text-gray-500 font-mono">{tenant.slug}</span>}
      </div>

      <BaaCard tenantId={tenantId} />
      <PlanAndFeaturesCard tenantId={tenantId} />
      <DicomEndpointsCard tenantId={tenantId} />
      <ApiKeysCard tenantId={tenantId} />
      <UsersCard tenantId={tenantId} />
      {tenant && <LimitsAndPurgeCard tenantId={tenantId} slug={tenant.slug} maxUsersNow={tenant.max_users ?? null} />}
    </div>
  );
}
