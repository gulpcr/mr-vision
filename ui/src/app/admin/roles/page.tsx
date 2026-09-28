"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type RoleDef } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { KeyRound, Plus, Save, Trash2, Lock, Copy } from "lucide-react";

type CatalogEntry = { key: string; description: string; in_plan?: boolean };

const RESOURCE_LABELS: Record<string, string> = {
  study: "Studies & worklist",
  job: "AI jobs",
  result: "Results & reports",
  alert: "Critical alerts",
  patient: "Patients",
  dashboard: "Dashboards",
  user: "Users",
  role: "Roles",
  settings: "Workspace",
  config: "Configuration",
  audit: "Audit",
  data: "Data",
};

export default function RolesPage() {
  const { can, refresh } = useAuth();
  const [roles, setRoles] = useState<RoleDef[]>([]);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Set<string>>(new Set());
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<RoleDef | null>(null);

  const load = useCallback(async () => {
    try {
      const [r, c] = await Promise.all([api.roles.list(), api.roles.catalog()]);
      setRoles(r);
      setCatalog(c.permissions);
      setSelectedId((cur) => cur ?? r[0]?.id ?? null);
    } catch (e: any) {
      setError(e.message || "Failed to load roles");
    }
  }, []);

  const allowed = can("role.manage");
  useEffect(() => {
    if (allowed) load();
  }, [allowed, load]);

  const selected = roles.find((r) => r.id === selectedId) ?? null;
  useEffect(() => {
    setDraft(new Set(selected?.permissions ?? []));
    setSaved(false);
  }, [selected]);

  const grouped = useMemo(() => {
    const groups: Record<string, CatalogEntry[]> = {};
    for (const entry of catalog) {
      const resource = entry.key.split(".")[0];
      (groups[resource] ??= []).push(entry);
    }
    return groups;
  }, [catalog]);

  if (!allowed) {
    return <EmptyState icon={KeyRound} title="You don't have permission to manage roles" />;
  }

  const isWildcard = draft.has("*");
  const lockedAdmin = selected?.name === "admin";
  const dirty =
    !!selected &&
    (draft.size !== (selected.permissions?.length ?? 0) || Array.from(draft).some((p) => !selected.permissions.includes(p)));

  const toggle = (key: string) => {
    if (lockedAdmin) return;
    setDraft((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
    setSaved(false);
  };

  const save = async () => {
    if (!selected) return;
    setError(null);
    try {
      await api.roles.update(selected.id, { permissions: Array.from(draft) });
      await load();
      refresh();
      setSaved(true);
    } catch (e: any) {
      setError(e.message || "Failed to save role");
    }
  };

  const create = async (copyFrom?: RoleDef) => {
    const name = (newName || (copyFrom ? `${copyFrom.name}_copy` : "")).trim().toLowerCase().replace(/\s+/g, "_");
    if (!name) return;
    setError(null);
    try {
      const role = await api.roles.create({
        name,
        permissions: (copyFrom?.permissions ?? []).filter((p) => p !== "*"),
      });
      setNewName("");
      await load();
      setSelectedId(role.id);
    } catch (e: any) {
      setError(e.message || "Failed to create role");
    }
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <KeyRound className="w-7 h-7 text-primary-600" />
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Roles</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            What each role can do in this workspace. Changes apply to everyone holding the role right away.
          </p>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-4" />}

      <div className="grid grid-cols-1 lg:grid-cols-[18rem_1fr] gap-6">
        <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-3 h-fit">
          <ul className="space-y-1">
            {roles.map((r) => (
              <li key={r.id}>
                <button
                  onClick={() => setSelectedId(r.id)}
                  className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm text-left ${
                    r.id === selectedId
                      ? "bg-primary-50 dark:bg-primary-950 text-primary-700 dark:text-primary-300 font-medium"
                      : "hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-300"
                  }`}
                >
                  <span>{r.name}</span>
                  {r.is_system && <span className="text-[10px] uppercase tracking-wider text-gray-400">system</span>}
                </button>
              </li>
            ))}
          </ul>
          <form
            onSubmit={(e) => { e.preventDefault(); create(); }}
            className="flex gap-2 mt-3 pt-3 border-t border-gray-100 dark:border-gray-800"
          >
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="new_role_name"
              pattern="[a-zA-Z0-9_ -]{2,64}"
              className="flex-1 min-w-0 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5"
              aria-label="New role name"
            />
            <button type="submit" className="p-2 rounded-lg bg-primary-600 text-white hover:bg-primary-700" aria-label="Create role">
              <Plus className="w-4 h-4" />
            </button>
          </form>
        </div>

        {selected ? (
          <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
              <div>
                <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{selected.name}</h2>
                {lockedAdmin && (
                  <p className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 mt-1">
                    <Lock className="w-3 h-3" /> The admin role always has full access to this workspace.
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => create(selected)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
                >
                  <Copy className="w-4 h-4" /> Clone
                </button>
                {!selected.is_system && (
                  <button
                    onClick={() => setPendingDelete(selected)}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg text-red-600 hover:bg-red-50 dark:hover:bg-red-950"
                  >
                    <Trash2 className="w-4 h-4" /> Delete
                  </button>
                )}
                <button
                  onClick={save}
                  disabled={!dirty || lockedAdmin}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-40"
                >
                  <Save className="w-4 h-4" /> {saved ? "Saved" : "Save"}
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {Object.entries(grouped).map(([resource, entries]) => (
                <fieldset key={resource} className="border border-gray-100 dark:border-gray-800 rounded-lg p-3">
                  <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                    {RESOURCE_LABELS[resource] ?? resource}
                  </legend>
                  <div className="space-y-2">
                    {entries.map((entry) => (
                      <label key={entry.key} className="flex items-start gap-2 text-sm cursor-pointer">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={isWildcard || draft.has(entry.key)}
                          disabled={lockedAdmin || isWildcard}
                          onChange={() => toggle(entry.key)}
                        />
                        <span>
                          <span className="font-mono text-xs text-gray-700 dark:text-gray-300">{entry.key}</span>
                          {entry.in_plan === false && (
                            <span className="ml-1.5 text-[10px] uppercase tracking-wider text-amber-600">not in your plan</span>
                          )}
                          <span className="block text-xs text-gray-500 dark:text-gray-400">{entry.description}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              ))}
            </div>
          </div>
        ) : (
          <EmptyState icon={KeyRound} title="Select a role" />
        )}
      </div>

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingDelete !== null}
        title="Delete role"
        consequence={pendingDelete ? `Delete the "${pendingDelete.name}" role? Roles still assigned to users cannot be deleted.` : ""}
        confirmLabel="Delete"
        onConfirm={async () => {
          if (!pendingDelete) return;
          try {
            await api.roles.remove(pendingDelete.id);
            setSelectedId(null);
            await load();
          } catch (e: any) {
            setError(e.message || "Failed to delete role");
          }
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
