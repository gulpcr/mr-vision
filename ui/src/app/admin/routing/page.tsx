"use client";

import { useState } from "react";
import { useRoutingRules, useUsecases } from "@/lib/hooks";
import { api } from "@/lib/api";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { EmptyState } from "@/components/ui/EmptyState";
import { GitBranch, Plus, Trash2, Code2, ListChecks } from "lucide-react";

interface OverrideRule {
  usecase_name: string;
  body_parts: string[];
  study_description_patterns: string[];
  series_description_patterns: string[];
  modality: string;
  priority: number;
  enabled: boolean;
}

type EditorMode = "structured" | "json";

function csvToList(s: string): string[] {
  return s.split(",").map((v) => v.trim()).filter(Boolean);
}

function listToCsv(v: string[]): string {
  return (v || []).join(", ");
}

function blankRule(defaultUsecase: string): OverrideRule {
  return {
    usecase_name: defaultUsecase,
    body_parts: [],
    study_description_patterns: [],
    series_description_patterns: [],
    modality: "MR",
    priority: 100,
    enabled: true,
  };
}

export default function RoutingAdminPage() {
  const { data: rulesData, isLoading, mutate } = useRoutingRules();
  const { data: usecases } = useUsecases();
  const [editing, setEditing] = useState(false);
  const [mode, setMode] = useState<EditorMode>("structured");
  const [draft, setDraft] = useState<OverrideRule[]>([]);
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pendingSave, setPendingSave] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedOverrideCount, setSavedOverrideCount] = useState<number | null>(null);

  const effectiveRules = rulesData?.routing_rules || {};
  const currentOverrides: OverrideRule[] = (rulesData?.site_overrides as OverrideRule[]) || [];

  const startEditing = () => {
    setDraft(currentOverrides.map((r) => ({ ...r })));
    setJsonText(JSON.stringify(currentOverrides, null, 2));
    setJsonError(null);
    setMode("structured");
    setEditing(true);
  };

  const cancelEditing = () => {
    setEditing(false);
    setJsonError(null);
  };

  const switchToJson = () => {
    setJsonText(JSON.stringify(draft, null, 2));
    setMode("json");
  };

  const switchToStructured = () => {
    try {
      const parsed = JSON.parse(jsonText);
      if (!Array.isArray(parsed)) throw new Error("Must be an array of rule objects");
      setDraft(parsed);
      setJsonError(null);
      setMode("structured");
    } catch (e: any) {
      setJsonError(e.message || "Invalid JSON");
    }
  };

  const updateRule = (idx: number, patch: Partial<OverrideRule>) => {
    setDraft((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };

  const removeRule = (idx: number) => {
    setDraft((prev) => prev.filter((_, i) => i !== idx));
  };

  const addRule = () => {
    setDraft((prev) => [...prev, blankRule(usecases?.[0]?.name || "")]);
  };

  // Resolve the draft that will actually be saved — reads from the JSON
  // textarea if that's the active mode, so Save works from either view.
  // Resolved once when the confirm dialog opens, so the diff preview and the
  // actual save always agree (no re-parsing drift between the two).
  const [confirmedDraft, setConfirmedDraft] = useState<OverrideRule[] | null>(null);

  const resolveDraftForSave = (): OverrideRule[] | null => {
    if (mode === "json") {
      try {
        const parsed = JSON.parse(jsonText);
        if (!Array.isArray(parsed)) throw new Error("Must be an array of rule objects");
        setJsonError(null);
        return parsed;
      } catch (e: any) {
        setJsonError(e.message || "Invalid JSON");
        return null;
      }
    }
    return draft;
  };

  const removedCount = confirmedDraft ? Math.max(0, currentOverrides.length - confirmedDraft.length) : 0;
  const willEmpty = currentOverrides.length > 0 && confirmedDraft !== null && confirmedDraft.length === 0;

  const handleSaveClick = () => {
    const toSave = resolveDraftForSave();
    if (toSave === null) return;
    setConfirmedDraft(toSave);
    setPendingSave(true);
  };

  const doSave = async () => {
    if (confirmedDraft === null) return;
    setSaveError(null);
    await api.admin.updateRoutingRules(confirmedDraft);
    setSavedOverrideCount(confirmedDraft.length);
    setEditing(false);
    mutate();
  };

  if (isLoading) return <p className="text-gray-500 dark:text-gray-400 p-4">Loading...</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <GitBranch className="w-7 h-7 text-primary-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Routing Rules</h1>
        </div>
        {!editing && (
          <button
            onClick={startEditing}
            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
          >
            Edit Site Overrides
          </button>
        )}
      </div>

      {savedOverrideCount !== null && !editing && (
        <div className="mb-4 text-sm text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg px-4 py-2.5">
          Saved — {savedOverrideCount} site override rule{savedOverrideCount === 1 ? "" : "s"} now in effect.
        </div>
      )}
      {saveError && <ErrorBanner message={saveError} onDismiss={() => setSaveError(null)} className="mb-4" />}

      {editing && (
        <div className="mb-6 bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-medium text-gray-900 dark:text-gray-100">Site Routing Overrides</h3>
            <div className="flex rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden text-xs">
              <button
                onClick={switchToStructured}
                className={`flex items-center gap-1.5 px-3 py-1.5 transition-colors ${
                  mode === "structured" ? "bg-primary-600 text-white" : "text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
                }`}
              >
                <ListChecks className="w-3.5 h-3.5" /> Structured
              </button>
              <button
                onClick={switchToJson}
                className={`flex items-center gap-1.5 px-3 py-1.5 transition-colors ${
                  mode === "json" ? "bg-primary-600 text-white" : "text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
                }`}
              >
                <Code2 className="w-3.5 h-3.5" /> Advanced (JSON)
              </button>
            </div>
          </div>

          {mode === "structured" ? (
            <div className="space-y-3">
              {draft.length === 0 && (
                <p className="text-sm text-gray-400 dark:text-gray-500 italic py-4 text-center">
                  No override rules in this draft — Save will remove all site overrides.
                </p>
              )}
              {draft.map((rule, idx) => (
                <div key={idx} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="grid grid-cols-2 gap-3 flex-1">
                      <label className="text-xs">
                        <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Use Case</span>
                        <select
                          value={rule.usecase_name}
                          onChange={(e) => updateRule(idx, { usecase_name: e.target.value })}
                          className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5"
                        >
                          {(usecases || []).map((uc) => (
                            <option key={uc.name} value={uc.name}>{uc.name}</option>
                          ))}
                        </select>
                      </label>
                      <label className="text-xs">
                        <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Modality</span>
                        <input
                          value={rule.modality}
                          onChange={(e) => updateRule(idx, { modality: e.target.value })}
                          className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5"
                        />
                      </label>
                      <label className="text-xs col-span-2">
                        <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Body Parts (comma-separated)</span>
                        <input
                          value={listToCsv(rule.body_parts)}
                          onChange={(e) => updateRule(idx, { body_parts: csvToList(e.target.value) })}
                          placeholder="BRAIN, HEAD"
                          className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5"
                        />
                      </label>
                      <label className="text-xs col-span-2">
                        <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Study Description Patterns (regex, comma-separated)</span>
                        <input
                          value={listToCsv(rule.study_description_patterns)}
                          onChange={(e) => updateRule(idx, { study_description_patterns: csvToList(e.target.value) })}
                          className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5 font-mono"
                        />
                      </label>
                      <label className="text-xs col-span-2">
                        <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Series Description Patterns (regex, comma-separated)</span>
                        <input
                          value={listToCsv(rule.series_description_patterns)}
                          onChange={(e) => updateRule(idx, { series_description_patterns: csvToList(e.target.value) })}
                          className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5 font-mono"
                        />
                      </label>
                      <label className="text-xs">
                        <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Priority</span>
                        <input
                          type="number"
                          value={rule.priority}
                          onChange={(e) => updateRule(idx, { priority: Number(e.target.value) })}
                          className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-2 py-1.5"
                        />
                      </label>
                      <label className="text-xs flex items-center gap-2 mt-5">
                        <input
                          type="checkbox"
                          checked={rule.enabled}
                          onChange={(e) => updateRule(idx, { enabled: e.target.checked })}
                        />
                        <span className="text-gray-600 dark:text-gray-300">Enabled</span>
                      </label>
                    </div>
                    <button
                      onClick={() => removeRule(idx)}
                      aria-label={`Remove override rule ${idx + 1}`}
                      className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950 rounded-lg transition-colors shrink-0"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
              <button
                onClick={addRule}
                className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-primary-700 border border-primary-200 dark:border-primary-800 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-950 transition-colors"
              >
                <Plus className="w-4 h-4" /> Add Rule
              </button>
            </div>
          ) : (
            <div>
              <textarea
                value={jsonText}
                onChange={(e) => setJsonText(e.target.value)}
                className="w-full h-64 font-mono text-xs border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-primary-500"
                placeholder='[{"usecase_name": "brain_mri", "body_parts": ["BRAIN"], "priority": 100}]'
              />
              {jsonError && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{jsonError}</p>}
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
                Advanced fallback — an array of routing rule override objects, each with a usecase_name.
              </p>
            </div>
          )}

          <div className="flex gap-2 mt-4 pt-4 border-t border-gray-100 dark:border-gray-800">
            <button
              onClick={handleSaveClick}
              className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
            >
              Save Site Overrides
            </button>
            <button
              onClick={cancelEditing}
              className="px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        tier="modal"
        danger={willEmpty || removedCount > 0}
        open={pendingSave}
        title="Save Site Routing Overrides"
        consequence={
          willEmpty
            ? `This removes all ${currentOverrides.length} existing site override rule${currentOverrides.length === 1 ? "" : "s"}. Routing will fall back entirely to each use case's base routing_rules.yaml. This cannot be undone from this screen.`
            : removedCount > 0
            ? `This replaces the current ${currentOverrides.length} override rule${currentOverrides.length === 1 ? "" : "s"} with ${confirmedDraft?.length ?? 0} — ${removedCount} rule${removedCount === 1 ? "" : "s"} will be removed.`
            : `This replaces the current ${currentOverrides.length} override rule${currentOverrides.length === 1 ? "" : "s"} with ${confirmedDraft?.length ?? 0} rule(s).`
        }
        confirmLabel="Save"
        onConfirm={async () => {
          await doSave();
          setPendingSave(false);
        }}
        onCancel={() => setPendingSave(false)}
      />

      <div className="space-y-4">
        {Object.entries(effectiveRules).map(([ucName, ucRules]) => (
          <div key={ucName} className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
            <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-3">
              {ucName.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
            </h2>
            {(ucRules as any[]).map((rule: any, idx: number) => (
              <div key={idx} className="border border-gray-100 dark:border-gray-800 rounded-lg p-3 mb-2 text-sm">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <span className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Body Parts</span>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {(rule.body_parts || []).map((bp: string) => (
                        <span key={bp} className="bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 px-2 py-0.5 rounded-full text-xs font-medium">
                          {bp}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div>
                    <span className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Modality</span>
                    <p className="text-xs mt-1 text-gray-700 dark:text-gray-300">{rule.modality || "MR"}</p>
                  </div>
                  {rule.study_description_patterns?.length > 0 && (
                    <div className="col-span-2">
                      <span className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Study Patterns</span>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {rule.study_description_patterns.map((p: string, i: number) => (
                          <code key={i} className="bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-2 py-0.5 rounded text-xs">
                            {p}
                          </code>
                        ))}
                      </div>
                    </div>
                  )}
                  {rule.series_description_patterns?.length > 0 && (
                    <div className="col-span-2">
                      <span className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Series Patterns</span>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {rule.series_description_patterns.map((p: string, i: number) => (
                          <code key={i} className="bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-2 py-0.5 rounded text-xs">
                            {p}
                          </code>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-4 text-xs text-gray-400 dark:text-gray-500">
                  <span>Priority: {rule.priority || 0}</span>
                  <span className={rule.enabled !== false ? "text-green-600 dark:text-green-400" : "text-gray-400 dark:text-gray-500"}>
                    {rule.enabled !== false ? "Enabled" : "Disabled"}
                  </span>
                </div>
              </div>
            ))}
            {(ucRules as any[]).length === 0 && (
              <p className="text-sm text-gray-400 dark:text-gray-500">No routing rules defined</p>
            )}
          </div>
        ))}
        {Object.keys(effectiveRules).length === 0 && (
          <EmptyState icon={GitBranch} title="No routing rules configured" />
        )}
      </div>
    </div>
  );
}
