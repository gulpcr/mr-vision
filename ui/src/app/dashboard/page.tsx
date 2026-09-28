"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type Dashboard,
  type DashboardVersion,
  type DashboardWidget,
  type WidgetData,
  type WidgetType,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { WidgetBody } from "@/components/dashboard/WidgetBody";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import {
  LayoutDashboard, Pencil, Plus, Save, X, Copy, Share2, History, Trash2, Settings2,
  ArrowLeft, ArrowRight, Minus, RefreshCw,
} from "lucide-react";

const PERIODS = [
  { value: "", label: "Widget defaults" },
  { value: "1d", label: "Today" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
];
const COLS = 12;
const ROW_PX = 64;

/** Re-derive grid x/y from widget order + sizes (row packing), so saved layouts are
 * always valid for the backend's 12-column validation. */
function pack(widgets: DashboardWidget[]): DashboardWidget[] {
  let x = 0;
  let y = 0;
  let rowH = 0;
  return widgets.map((w) => {
    const width = Math.max(1, Math.min(COLS, w.position.w));
    const height = Math.max(1, Math.min(12, w.position.h));
    if (x + width > COLS) {
      x = 0;
      y += rowH;
      rowH = 0;
    }
    const placed = { ...w, position: { x, y, w: width, h: height } };
    x += width;
    rowH = Math.max(rowH, height);
    return placed;
  });
}

function newWidgetId() {
  return `w_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}

function ConfigDialog({
  widget, spec, onSave, onClose,
}: {
  widget: DashboardWidget; spec: WidgetType | undefined;
  onSave: (w: DashboardWidget) => void; onClose: () => void;
}) {
  const [title, setTitle] = useState(widget.title);
  const [config, setConfig] = useState<Record<string, any>>({ ...widget.config });
  const inputCls = "w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2";
  return (
    <Modal open onClose={onClose} title={`Configure: ${spec?.label ?? widget.type}`} size="sm">
      <div className="space-y-3">
        <label className="block text-sm">
          <span className="text-gray-600 dark:text-gray-300">Title</span>
          <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        {(spec?.config_schema ?? []).map((f) => (
          <label key={f.key} className="block text-sm">
            <span className="text-gray-600 dark:text-gray-300">{f.label}</span>
            {f.type === "select" ? (
              <select className={inputCls} value={config[f.key] ?? f.default}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}>
                {(f.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            ) : f.type === "number" ? (
              <input type="number" className={inputCls} min={f.min} max={f.max} value={config[f.key] ?? f.default}
                onChange={(e) => setConfig({ ...config, [f.key]: Number(e.target.value) })} />
            ) : f.type === "text" ? (
              <textarea rows={4} className={inputCls} value={config[f.key] ?? ""}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })} />
            ) : (
              <input className={inputCls} value={config[f.key] ?? ""}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })} />
            )}
          </label>
        ))}
        <div className="flex justify-end gap-2 pt-2">
          <button onClick={onClose} className="px-4 py-2 text-sm rounded-lg bg-gray-100 dark:bg-gray-800">Cancel</button>
          <button onClick={() => onSave({ ...widget, title, config })}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700">Apply</button>
        </div>
      </div>
    </Modal>
  );
}

export default function DashboardPage() {
  const { can, user } = useAuth();
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [types, setTypes] = useState<WidgetType[]>([]);
  const [data, setData] = useState<Record<string, WidgetData>>({});
  const [period, setPeriod] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<DashboardWidget[]>([]);
  const [draftName, setDraftName] = useState("");
  const [picker, setPicker] = useState(false);
  const [configuring, setConfiguring] = useState<DashboardWidget | null>(null);
  const [versions, setVersions] = useState<DashboardVersion[] | null>(null);
  const [pendingDelete, setPendingDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const dataTimer = useRef<number | null>(null);

  const canManage = can("dashboard.manage");
  const canShare = can("dashboard.share");
  const active = dashboards.find((d) => d.id === activeId) ?? null;
  const typeOf = useMemo(() => Object.fromEntries(types.map((t) => [t.type, t])), [types]);

  const loadList = useCallback(async (selectId?: string) => {
    try {
      const [list, catalog] = await Promise.all([api.dashboards.list(), api.dashboards.widgetTypes()]);
      setDashboards(list);
      setTypes(catalog);
      setActiveId((cur) => {
        if (selectId) return selectId;
        if (cur && list.some((d) => d.id === cur)) return cur;
        try {
          const remembered = localStorage.getItem(`dashboard:${user?.id}`);
          if (remembered && list.some((d) => d.id === remembered)) return remembered;
        } catch { /* storage unavailable */ }
        return list[0]?.id ?? null;
      });
    } catch (e: any) {
      setError(e.message || "Failed to load dashboards");
    } finally {
      setLoaded(true);
    }
  }, [user?.id]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  const loadData = useCallback(async () => {
    if (!activeId) return;
    try {
      const res = await api.dashboards.data(activeId, period ? { period } : undefined);
      setData(res.data);
    } catch (e: any) {
      setError(e.message || "Failed to load dashboard data");
    }
  }, [activeId, period]);

  useEffect(() => {
    if (!activeId || editing) return;
    try { localStorage.setItem(`dashboard:${user?.id}`, activeId); } catch { /* ignore */ }
    loadData();
    const every = Math.max(30, active?.refresh_interval ?? 300) * 1000;
    dataTimer.current = window.setInterval(loadData, every);
    return () => {
      if (dataTimer.current) window.clearInterval(dataTimer.current);
    };
  }, [activeId, period, editing, loadData, active?.refresh_interval, user?.id]);

  const startEdit = () => {
    if (!active) return;
    setDraft(active.widgets.map((w) => ({ ...w, position: { ...w.position } })));
    setDraftName(active.name);
    setEditing(true);
  };

  const customise = async () => {
    if (!active) return;
    try {
      const copy = await api.dashboards.clone(active.id, `${active.name} (mine)`);
      await loadList(copy.id);
      setDraft(copy.widgets);
      setDraftName(copy.name);
      setEditing(true);
    } catch (e: any) {
      setError(e.message || "Failed to copy dashboard");
    }
  };

  const createBlank = async () => {
    try {
      const d = await api.dashboards.create({ name: "New dashboard", widgets: [] });
      await loadList(d.id);
      setDraft([]);
      setDraftName(d.name);
      setEditing(true);
    } catch (e: any) {
      setError(e.message || "Failed to create dashboard");
    }
  };

  const save = async () => {
    if (!active) return;
    try {
      await api.dashboards.update(active.id, { name: draftName.trim() || active.name, widgets: pack(draft) });
      setEditing(false);
      await loadList(active.id);
    } catch (e: any) {
      setError(e.message || "Failed to save dashboard");
    }
  };

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= draft.length) return;
    const next = [...draft];
    [next[i], next[j]] = [next[j], next[i]];
    setDraft(next);
  };
  const resize = (i: number, dw: number, dh: number) => {
    setDraft(draft.map((w, k) => k === i ? {
      ...w, position: {
        ...w.position,
        w: Math.max(2, Math.min(COLS, w.position.w + dw)),
        h: Math.max(2, Math.min(10, w.position.h + dh)),
      },
    } : w));
  };
  const addWidget = (t: WidgetType) => {
    setDraft([...draft, {
      id: newWidgetId(), type: t.type, title: t.label,
      config: Object.fromEntries(t.config_schema.map((f) => [f.key, f.default])),
      position: { x: 0, y: 0, w: t.default_size.w, h: t.default_size.h },
    }]);
    setPicker(false);
  };

  const widgets = editing ? draft : pack(active?.widgets ?? []);

  if (loaded && dashboards.length === 0) {
    return (
      <EmptyState
        icon={LayoutDashboard}
        title="No dashboards yet"
        description="Your workspace has no dashboard for your role."
        action={canManage ? (
          <button onClick={createBlank} className="mt-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary-600 text-white">
            Create a dashboard
          </button>
        ) : undefined}
      />
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div className="flex items-center gap-3 min-w-0">
          <LayoutDashboard className="w-7 h-7 text-primary-600 shrink-0" />
          {editing ? (
            <input value={draftName} onChange={(e) => setDraftName(e.target.value)} aria-label="Dashboard name"
              className="text-xl font-bold bg-transparent border-b border-gray-300 dark:border-gray-600 focus:outline-none" />
          ) : (
            <select
              value={activeId ?? ""}
              onChange={(e) => setActiveId(e.target.value)}
              aria-label="Choose dashboard"
              className="text-xl font-bold bg-transparent text-gray-900 dark:text-gray-100 focus:outline-none max-w-[28rem] truncate"
            >
              {dashboards.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}{d.is_default ? ` · ${d.role_default} default` : d.is_shared && !d.is_owner ? " · shared" : ""}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!editing && (
            <>
              <select value={period} onChange={(e) => setPeriod(e.target.value)} aria-label="Period"
                className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5">
                {PERIODS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
              <button onClick={loadData} title="Refresh" aria-label="Refresh"
                className="p-2 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700">
                <RefreshCw className="w-4 h-4" />
              </button>
              {canManage && active?.can_edit && (
                <button onClick={startEdit} className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700">
                  <Pencil className="w-4 h-4" /> Edit
                </button>
              )}
              {canManage && active && !active.can_edit && (
                <button onClick={customise} className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700">
                  <Copy className="w-4 h-4" /> Customise
                </button>
              )}
              {canManage && (
                <button onClick={createBlank} title="New dashboard" aria-label="New dashboard"
                  className="p-2 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700">
                  <Plus className="w-4 h-4" />
                </button>
              )}
              {active?.is_owner && canShare && (
                <button
                  onClick={async () => {
                    await api.dashboards.update(active.id, { is_shared: !active.is_shared }).catch((e) => setError(e.message));
                    loadList(active.id);
                  }}
                  className={`flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg ${active.is_shared ? "bg-primary-50 dark:bg-primary-950 text-primary-700 dark:text-primary-300" : "bg-gray-100 dark:bg-gray-800"}`}
                >
                  <Share2 className="w-4 h-4" /> {active.is_shared ? "Shared" : "Share"}
                </button>
              )}
              {active?.can_edit && (
                <button
                  onClick={async () => setVersions(await api.dashboards.versions(active.id).catch(() => []))}
                  title="Version history" aria-label="Version history"
                  className="p-2 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
                >
                  <History className="w-4 h-4" />
                </button>
              )}
              {active?.is_owner && (
                <button onClick={() => setPendingDelete(true)} title="Delete dashboard" aria-label="Delete dashboard"
                  className="p-2 rounded-lg text-red-600 hover:bg-red-50 dark:hover:bg-red-950">
                  <Trash2 className="w-4 h-4" />
                </button>
              )}
            </>
          )}
          {editing && (
            <>
              <button onClick={() => setPicker(true)} className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700">
                <Plus className="w-4 h-4" /> Add widget
              </button>
              <button onClick={() => setEditing(false)} className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800">
                <X className="w-4 h-4" /> Cancel
              </button>
              <button onClick={save} className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700">
                <Save className="w-4 h-4" /> Save
              </button>
            </>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-4" />}
      {active?.is_default && !editing && (
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          This is your workspace&apos;s default dashboard for the <strong>{active.role_default}</strong> role
          {active.can_edit ? " — edits apply to everyone with that role." : ". Use Customise to make your own copy."}
        </p>
      )}

      <div
        className="grid gap-4"
        style={{ gridTemplateColumns: `repeat(${COLS}, minmax(0, 1fr))`, gridAutoRows: `${ROW_PX}px` }}
      >
        {widgets.map((w, i) => (
          <section
            key={w.id}
            className={`bg-white dark:bg-surface rounded-xl border ${editing ? "border-dashed border-primary-300 dark:border-primary-700" : "border-gray-200 dark:border-gray-700"} shadow-sm p-4 flex flex-col min-h-0 overflow-hidden`}
            style={{ gridColumn: `span ${Math.min(COLS, w.position.w)} / span ${Math.min(COLS, w.position.w)}`, gridRow: `span ${w.position.h} / span ${w.position.h}` }}
          >
            <header className="flex items-center justify-between gap-2 mb-2">
              <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 truncate">{w.title}</h3>
              {editing && (
                <div className="flex items-center gap-0.5 shrink-0">
                  <button onClick={() => move(i, -1)} aria-label="Move earlier" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800"><ArrowLeft className="w-3.5 h-3.5" /></button>
                  <button onClick={() => move(i, 1)} aria-label="Move later" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800"><ArrowRight className="w-3.5 h-3.5" /></button>
                  <button onClick={() => resize(i, -1, 0)} aria-label="Narrower" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800"><Minus className="w-3.5 h-3.5" /></button>
                  <button onClick={() => resize(i, 1, 0)} aria-label="Wider" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800"><Plus className="w-3.5 h-3.5" /></button>
                  <button onClick={() => resize(i, 0, 1)} aria-label="Taller" className="px-1 text-[10px] rounded hover:bg-gray-100 dark:hover:bg-gray-800">↕+</button>
                  <button onClick={() => resize(i, 0, -1)} aria-label="Shorter" className="px-1 text-[10px] rounded hover:bg-gray-100 dark:hover:bg-gray-800">↕−</button>
                  <button onClick={() => setConfiguring(w)} aria-label="Configure" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800"><Settings2 className="w-3.5 h-3.5" /></button>
                  <button onClick={() => setDraft(draft.filter((_, k) => k !== i))} aria-label="Remove widget" className="p-1 rounded text-red-500 hover:bg-red-50 dark:hover:bg-red-950"><Trash2 className="w-3.5 h-3.5" /></button>
                </div>
              )}
            </header>
            <div className="flex-1 min-h-0 overflow-auto">
              {editing ? (
                <p className="text-xs text-gray-400">{typeOf[w.type]?.description ?? w.type} · {w.position.w}×{w.position.h}</p>
              ) : (
                <WidgetBody type={w.type} result={data[w.id]} />
              )}
            </div>
          </section>
        ))}
        {editing && widgets.length === 0 && (
          <div className="col-span-12 text-center text-sm text-gray-400 py-12">Add widgets to build this dashboard.</div>
        )}
      </div>

      <Modal open={picker} onClose={() => setPicker(false)} title="Add a widget" size="lg">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-[60vh] overflow-y-auto">
          {types.map((t) => (
            <button key={t.type} onClick={() => addWidget(t)}
              className="text-left p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:border-primary-400 hover:bg-primary-50/40 dark:hover:bg-primary-950/40">
              <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{t.label}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">{t.description}</p>
            </button>
          ))}
        </div>
      </Modal>

      {configuring && (
        <ConfigDialog
          widget={configuring}
          spec={typeOf[configuring.type]}
          onClose={() => setConfiguring(null)}
          onSave={(w) => {
            setDraft(draft.map((d) => (d.id === w.id ? w : d)));
            setConfiguring(null);
          }}
        />
      )}

      <Modal open={versions !== null} onClose={() => setVersions(null)} title="Version history" size="sm">
        {versions && versions.length === 0 ? (
          <p className="text-sm text-gray-500">No earlier versions yet.</p>
        ) : (
          <ul className="divide-y divide-gray-100 dark:divide-gray-800 text-sm">
            {(versions ?? []).map((v) => (
              <li key={v.version} className="py-2 flex items-center justify-between gap-2">
                <span>
                  v{v.version} · {v.widget_count} widgets
                  <span className="block text-xs text-gray-500">
                    {v.created_by} · {v.created_at ? new Date(v.created_at).toLocaleString() : ""}
                  </span>
                </span>
                <button
                  onClick={async () => {
                    if (!active) return;
                    await api.dashboards.restore(active.id, v.version).catch((e) => setError(e.message));
                    setVersions(null);
                    loadList(active.id);
                  }}
                  className="px-3 py-1 text-xs rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
                >
                  Restore
                </button>
              </li>
            ))}
          </ul>
        )}
      </Modal>

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingDelete}
        title="Delete dashboard"
        consequence={active ? `Delete "${active.name}"? This cannot be undone.` : ""}
        confirmLabel="Delete"
        onConfirm={async () => {
          if (active) {
            await api.dashboards.remove(active.id).catch((e) => setError(e.message));
            setActiveId(null);
            await loadList();
          }
          setPendingDelete(false);
        }}
        onCancel={() => setPendingDelete(false)}
      />
    </div>
  );
}
