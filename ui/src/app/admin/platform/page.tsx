"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  type PlatformAuditEntry,
  type PlatformOverview,
  type PlatformUser,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Table, Caption, Th } from "@/components/ui/Table";
import { Globe, Search, LogOut, Link2, RefreshCw } from "lucide-react";

type Tab = "tenants" | "audit" | "users";

function bytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <p className="text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400">{label}</p>
      <p className="text-2xl font-bold tabular-nums text-gray-900 dark:text-gray-100">{value}</p>
    </div>
  );
}

export default function PlatformPage() {
  const { can } = useAuth();
  const [tab, setTab] = useState<Tab>("tenants");
  const [overview, setOverview] = useState<PlatformOverview | null>(null);
  const [audit, setAudit] = useState<PlatformAuditEntry[]>([]);
  const [auditTenant, setAuditTenant] = useState("");
  const [auditAction, setAuditAction] = useState("");
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const allowed = can("tenant.manage");

  const loadOverview = useCallback(() => {
    api.platform.overview().then(setOverview).catch((e) => setError(e.message));
  }, []);
  const loadAudit = useCallback(() => {
    api.platform
      .audit({ tenant_id: auditTenant || undefined, action: auditAction || undefined, limit: 200 })
      .then(setAudit)
      .catch((e) => setError(e.message));
  }, [auditTenant, auditAction]);
  const loadUsers = useCallback(() => {
    api.platform.users(query).then(setUsers).catch((e) => setError(e.message));
  }, [query]);

  useEffect(() => {
    if (!allowed) return;
    if (tab === "tenants") loadOverview();
    if (tab === "audit") loadAudit();
    if (tab === "users") loadUsers();
  }, [allowed, tab, loadOverview, loadAudit, loadUsers]);

  if (!allowed) {
    return <EmptyState icon={Globe} title="Platform console" description="Requires platform-admin access." />;
  }

  const t = overview?.totals ?? {};

  return (
    <div>
      <div className="flex items-center justify-between gap-3 mb-6">
        <div className="flex items-center gap-3">
          <Globe className="w-7 h-7 text-primary-600" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Platform</h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">Every workspace on this installation.</p>
          </div>
        </div>
        <div className="flex gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-1">
          {(["tenants", "audit", "users"] as Tab[]).map((k) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-3 py-1.5 text-sm rounded-md capitalize ${tab === k ? "bg-white dark:bg-surface shadow-sm font-medium" : "text-gray-600 dark:text-gray-300"}`}>
              {k === "tenants" ? "Workspaces" : k === "audit" ? "Audit log" : "Users"}
            </button>
          ))}
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-4" />}
      {notice && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300 break-all">
          {notice}
        </div>
      )}

      {tab === "tenants" && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-5">
            <Stat label="Workspaces" value={t.tenants ?? "—"} />
            <Stat label="Users" value={t.users ?? "—"} />
            <Stat label="Studies (30 days)" value={t.studies_30d ?? "—"} />
            <Stat label="Failed AI jobs (7 days)" value={t.jobs_failed_7d ?? "—"} />
            <Stat label="Artifact storage" value={bytes(t.storage_bytes ?? 0)} />
          </div>
          <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
            <Table>
              <Caption>Usage per workspace</Caption>
              <thead>
                <tr className="border-b border-gray-100 dark:border-gray-800">
                  <Th>Workspace</Th><Th>Status</Th><Th>Plan</Th><Th>Users</Th><Th>Studies</Th>
                  <Th>30 days</Th><Th>Active jobs</Th><Th>Failed 7d</Th><Th>Storage</Th><Th>Last study</Th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50 dark:divide-gray-800 text-sm">
                {(overview?.tenants ?? []).map((r) => (
                  <tr key={r.tenant_id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                    <td className="py-2 px-4">
                      <Link href={`/admin/tenants/${r.tenant_id}`} className="font-medium text-primary-700 dark:text-primary-300 hover:underline">
                        {r.name}
                      </Link>
                      <span className="block text-xs font-mono text-gray-400">{r.slug}</span>
                    </td>
                    <td className="py-2 px-4 capitalize">{r.deleted_at ? "purged" : r.status}</td>
                    <td className="py-2 px-4">{r.plan}</td>
                    <td className="py-2 px-4 tabular-nums">{r.users}{r.max_users ? ` / ${r.max_users}` : ""}</td>
                    <td className="py-2 px-4 tabular-nums">{r.studies}</td>
                    <td className="py-2 px-4 tabular-nums">{r.studies_30d}</td>
                    <td className="py-2 px-4 tabular-nums">{r.jobs_active}</td>
                    <td className={`py-2 px-4 tabular-nums ${r.jobs_failed_7d ? "text-red-600" : ""}`}>{r.jobs_failed_7d}</td>
                    <td className="py-2 px-4 tabular-nums">{bytes(r.storage_bytes)}</td>
                    <td className="py-2 px-4 text-xs text-gray-500">{r.last_study_at ? new Date(r.last_study_at).toLocaleString() : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        </>
      )}

      {tab === "audit" && (
        <>
          <div className="flex flex-wrap gap-2 mb-3">
            <select value={auditTenant} onChange={(e) => setAuditTenant(e.target.value)} aria-label="Workspace"
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5">
              <option value="">All workspaces</option>
              {(overview?.tenants ?? []).map((r) => <option key={r.tenant_id} value={r.tenant_id}>{r.name}</option>)}
            </select>
            <input value={auditAction} onChange={(e) => setAuditAction(e.target.value)} placeholder="Action (e.g. user_login)"
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5" />
            <button onClick={loadAudit} className="p-2 rounded-lg bg-gray-100 dark:bg-gray-800" aria-label="Refresh audit">
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
          <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
            <Table>
              <Caption>Audit events across workspaces</Caption>
              <thead>
                <tr className="border-b border-gray-100 dark:border-gray-800">
                  <Th>Time</Th><Th>Workspace</Th><Th>Action</Th><Th>Entity</Th><Th>Actor</Th><Th>IP</Th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50 dark:divide-gray-800 text-xs">
                {audit.map((a) => (
                  <tr key={a.id}>
                    <td className="py-1.5 px-4 whitespace-nowrap">{a.timestamp ? new Date(a.timestamp).toLocaleString() : ""}</td>
                    <td className="py-1.5 px-4 font-mono">{a.tenant_id}</td>
                    <td className="py-1.5 px-4">{a.action}</td>
                    <td className="py-1.5 px-4 font-mono truncate max-w-[16rem]">{a.entity_type}:{a.entity_id}</td>
                    <td className="py-1.5 px-4">{a.actor}</td>
                    <td className="py-1.5 px-4">{a.client_ip ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        </>
      )}

      {tab === "users" && (
        <>
          <form onSubmit={(e) => { e.preventDefault(); loadUsers(); }} className="flex gap-2 mb-3">
            <div className="relative">
              <Search className="w-4 h-4 absolute left-2.5 top-2.5 text-gray-400" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Username, email or name"
                className="pl-8 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-1.5 w-72" />
            </div>
            <button type="submit" className="px-3 py-1.5 text-sm rounded-lg bg-primary-600 text-white">Search</button>
          </form>
          <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
            <Table>
              <Caption>Users across workspaces (metadata only)</Caption>
              <thead>
                <tr className="border-b border-gray-100 dark:border-gray-800">
                  <Th>User</Th><Th>Workspace</Th><Th>Role</Th><Th>Status</Th><Th>MFA</Th><Th className="w-24" />
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50 dark:divide-gray-800 text-sm">
                {users.map((u) => (
                  <tr key={u.id}>
                    <td className="py-2 px-4">
                      <span className="font-medium">{u.username}</span>
                      <span className="block text-xs text-gray-500">{u.email}</span>
                    </td>
                    <td className="py-2 px-4 font-mono text-xs">{u.tenant_id}</td>
                    <td className="py-2 px-4">{u.role}</td>
                    <td className="py-2 px-4 capitalize">{u.is_active ? u.status : "deactivated"}</td>
                    <td className="py-2 px-4">{u.totp_enabled ? "on" : "—"}</td>
                    <td className="py-2 px-4">
                      <div className="flex gap-1 justify-end">
                        <button title="Sign out everywhere" aria-label={`Sign ${u.username} out everywhere`}
                          onClick={async () => {
                            await api.platform.revokeSessions(u.id).catch((e) => setError(e.message));
                            setNotice(`${u.username} signed out everywhere.`);
                          }}
                          className="p-1.5 rounded-lg text-gray-400 hover:text-amber-600 hover:bg-amber-50 dark:hover:bg-amber-950">
                          <LogOut className="w-4 h-4" />
                        </button>
                        <button title="Password reset link" aria-label={`Password reset link for ${u.username}`}
                          onClick={async () => {
                            try {
                              const r = await api.platform.resetLink(u.id);
                              setNotice(`Reset link for ${u.username} (valid ${r.expires_in_hours ?? 3}h): ${r.reset_link}`);
                            } catch (e: any) { setError(e.message); }
                          }}
                          className="p-1.5 rounded-lg text-gray-400 hover:text-primary-600 hover:bg-primary-50 dark:hover:bg-primary-950">
                          <Link2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        </>
      )}
    </div>
  );
}
