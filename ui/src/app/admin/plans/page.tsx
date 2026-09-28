"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Plan } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Layers, Plus, Save, Trash2 } from "lucide-react";

type Catalog = { usecases: string[]; permissions: { key: string; description: string }[] };

interface Draft {
  display_name: string;
  description: string;
  default_max_users: string;
  allUsecases: boolean;
  usecases: Set<string>;
  allPermissions: boolean;
  permissions: Set<string>;
  is_active: boolean;
}

const inputCls =
  "w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2";

function toDraft(p: Plan): Draft {
  return {
    display_name: p.display_name,
    description: p.description ?? "",
    default_max_users: p.default_max_users ? String(p.default_max_users) : "",
    allUsecases: p.usecases === null,
    usecases: new Set(p.usecases ?? []),
    allPermissions: p.permissions.includes("*"),
    permissions: new Set(p.permissions.filter((x) => x !== "*")),
    is_active: p.is_active,
  };
}

function toggle(set: Set<string>, key: string): Set<string> {
  const next = new Set(set);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

export default function PlansPage() {
  const { can } = useAuth();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [newKey, setNewKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Plan | null>(null);

  const allowed = can("tenant.manage");

  const load = useCallback(async (select?: string) => {
    try {
      const [p, c] = await Promise.all([api.plans.list(), api.plans.catalog()]);
      setPlans(p);
      setCatalog(c);
      setSelected((cur) => select ?? cur ?? p[0]?.name ?? null);
    } catch (e: any) {
      setError(e.message || "Failed to load plans");
    }
  }, []);

  useEffect(() => {
    if (allowed) load();
  }, [allowed, load]);

  const plan = plans.find((p) => p.name === selected) ?? null;
  useEffect(() => {
    setDraft(plan ? toDraft(plan) : null);
    setSaved(false);
  }, [plan]);

  const permGroups = useMemo(() => {
    const groups: Record<string, { key: string; description: string }[]> = {};
    for (const p of catalog?.permissions ?? []) (groups[p.key.split(".")[0]] ??= []).push(p);
    return groups;
  }, [catalog]);

  if (!allowed) {
    return <EmptyState icon={Layers} title="Plans" description="Requires platform-admin access." />;
  }

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    const key = newKey.trim().toLowerCase().replace(/\s+/g, "-");
    if (!key) return;
    setError(null);
    try {
      await api.plans.create({ name: key, display_name: newKey.trim(), permissions: ["*"], usecases: null });
      setNewKey("");
      await load(key);
    } catch (err: any) {
      setError(err.message || "Failed to create plan");
    }
  };

  const save = async () => {
    if (!plan || !draft) return;
    setError(null);
    try {
      await api.plans.update(plan.name, {
        display_name: draft.display_name,
        description: draft.description || null,
        default_max_users: draft.default_max_users ? Number(draft.default_max_users) : null,
        usecases: draft.allUsecases ? null : Array.from(draft.usecases),
        permissions: draft.allPermissions ? ["*"] : Array.from(draft.permissions),
        is_active: draft.is_active,
      });
      await load(plan.name);
      setSaved(true);
    } catch (err: any) {
      setError(err.message || "Failed to save plan");
    }
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <Layers className="w-7 h-7 text-primary-600" />
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Plans</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            What each subscription includes. Changes apply at once to every tenant on the plan.
          </p>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-4" />}

      <div className="grid grid-cols-1 lg:grid-cols-[18rem_1fr] gap-6">
        <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-3 h-fit">
          <ul className="space-y-1">
            {plans.map((p) => (
              <li key={p.name}>
                <button
                  onClick={() => setSelected(p.name)}
                  className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm text-left ${
                    p.name === selected
                      ? "bg-primary-50 dark:bg-primary-950 text-primary-700 dark:text-primary-300 font-medium"
                      : "hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-300"
                  }`}
                >
                  <span className={p.is_active ? "" : "line-through opacity-60"}>{p.display_name}</span>
                  <span className="text-[10px] text-gray-400">{p.tenant_count} tenant{p.tenant_count === 1 ? "" : "s"}</span>
                </button>
              </li>
            ))}
          </ul>
          <form onSubmit={create} className="flex gap-2 mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
            <input
              value={newKey}
              onChange={(e) => setNewKey(e.target.value)}
              placeholder="New plan (e.g. Enterprise)"
              className="flex-1 min-w-0 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5"
              aria-label="New plan name"
            />
            <button type="submit" className="p-2 rounded-lg bg-primary-600 text-white hover:bg-primary-700" aria-label="Create plan">
              <Plus className="w-4 h-4" />
            </button>
          </form>
        </div>

        {plan && draft && catalog ? (
          <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 space-y-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{plan.display_name}</h2>
                <p className="text-xs font-mono text-gray-400">key: {plan.name}</p>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => setPendingDelete(plan)}
                  disabled={plan.tenant_count > 0}
                  title={plan.tenant_count > 0 ? "Move its tenants to another plan first" : "Delete plan"}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg text-red-600 hover:bg-red-50 dark:hover:bg-red-950 disabled:opacity-40"
                >
                  <Trash2 className="w-4 h-4" /> Delete
                </button>
                <button onClick={save}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700">
                  <Save className="w-4 h-4" /> {saved ? "Saved" : "Save"}
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <label className="block text-sm">
                <span className="text-gray-600 dark:text-gray-300">Display name</span>
                <input className={inputCls} value={draft.display_name}
                  onChange={(e) => setDraft({ ...draft, display_name: e.target.value })} />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600 dark:text-gray-300">Default seat limit (blank = unlimited)</span>
                <input type="number" min={1} className={inputCls} value={draft.default_max_users}
                  onChange={(e) => setDraft({ ...draft, default_max_users: e.target.value })} />
              </label>
              <label className="block text-sm md:col-span-2">
                <span className="text-gray-600 dark:text-gray-300">Description (shown when choosing a plan)</span>
                <textarea rows={2} className={inputCls} value={draft.description}
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={draft.is_active}
                  onChange={(e) => setDraft({ ...draft, is_active: e.target.checked })} />
                <span className="text-gray-700 dark:text-gray-300">Available for new tenants</span>
              </label>
            </div>

            <fieldset className="border border-gray-100 dark:border-gray-800 rounded-lg p-4">
              <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-gray-500">AI use cases</legend>
              <label className="flex items-center gap-2 text-sm mb-3">
                <input type="checkbox" checked={draft.allUsecases}
                  onChange={(e) => setDraft({ ...draft, allUsecases: e.target.checked })} />
                <span>All use cases, including ones added in future</span>
              </label>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                {catalog.usecases.map((u) => (
                  <label key={u} className={`flex items-center gap-2 text-sm ${draft.allUsecases ? "opacity-50" : ""}`}>
                    <input type="checkbox" disabled={draft.allUsecases}
                      checked={draft.allUsecases || draft.usecases.has(u)}
                      onChange={() => setDraft({ ...draft, usecases: toggle(draft.usecases, u) })} />
                    <span className="font-mono text-xs">{u}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="border border-gray-100 dark:border-gray-800 rounded-lg p-4">
              <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-gray-500">Permissions</legend>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                The most any role in a tenant on this plan can do — including the tenant&apos;s own admin.
                A role still needs the permission itself; the plan only sets the upper limit.
              </p>
              <label className="flex items-center gap-2 text-sm mb-3">
                <input type="checkbox" checked={draft.allPermissions}
                  onChange={(e) => setDraft({ ...draft, allPermissions: e.target.checked })} />
                <span>All permissions (no limit)</span>
              </label>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {Object.entries(permGroups).map(([group, entries]) => (
                  <div key={group} className={draft.allPermissions ? "opacity-50" : ""}>
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-400 mb-1">{group}</p>
                    {entries.map((p) => (
                      <label key={p.key} className="flex items-start gap-2 text-sm mb-1">
                        <input type="checkbox" className="mt-0.5" disabled={draft.allPermissions}
                          checked={draft.allPermissions || draft.permissions.has(p.key)}
                          onChange={() => setDraft({ ...draft, permissions: toggle(draft.permissions, p.key) })} />
                        <span>
                          <span className="font-mono text-xs">{p.key}</span>
                          <span className="block text-xs text-gray-500 dark:text-gray-400">{p.description}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                ))}
              </div>
            </fieldset>
          </div>
        ) : (
          <EmptyState icon={Layers} title="Select or create a plan" />
        )}
      </div>

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingDelete !== null}
        title="Delete plan"
        consequence={pendingDelete ? `Delete the "${pendingDelete.display_name}" plan?` : ""}
        confirmLabel="Delete"
        onConfirm={async () => {
          if (!pendingDelete) return;
          try {
            await api.plans.remove(pendingDelete.name);
            setSelected(null);
            await load();
          } catch (err: any) {
            setError(err.message || "Failed to delete plan");
          }
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
