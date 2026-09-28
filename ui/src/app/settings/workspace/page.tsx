"use client";

import { useEffect, useState } from "react";
import { api, type WorkspaceBranding, type WorkspaceSettings } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Palette, Save, Upload } from "lucide-react";

const inputCls =
  "w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2";

const SETTINGS_FIELDS: { key: keyof WorkspaceSettings; label: string; multiline?: boolean }[] = [
  { key: "institution_name", label: "Institution name (report heading)" },
  { key: "institution_address", label: "Address", multiline: true },
  { key: "report_header", label: "Report header text", multiline: true },
  { key: "report_footer", label: "Report footer text", multiline: true },
  { key: "signatory_name", label: "Reporting signatory" },
  { key: "signatory_title", label: "Signatory title" },
  { key: "signatory_qualifications", label: "Signatory qualifications" },
  { key: "secondary_signatory_name", label: "Second signatory (PET-CT reports)" },
  { key: "timezone", label: "Timezone (e.g. Asia/Karachi)" },
];

export default function WorkspaceSettingsPage() {
  const { can, workspace, refresh } = useAuth();
  const [branding, setBranding] = useState<WorkspaceBranding | null>(null);
  const [settings, setSettings] = useState<WorkspaceSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (workspace) {
      setBranding({ ...workspace.branding });
      setSettings({ ...workspace.settings });
    }
  }, [workspace]);

  if (!can("settings.manage")) {
    return <EmptyState icon={Palette} title="You don't have permission to edit workspace settings" />;
  }
  if (!branding || !settings) return null;

  const onLogo = (file: File | undefined) => {
    if (!file) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setError("Logo must be a PNG, JPEG or WebP image.");
      return;
    }
    if (file.size > 300_000) {
      setError("Logo must be smaller than 300 KB.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setBranding({ ...branding, logo_data_url: String(reader.result) });
    reader.readAsDataURL(file);
  };

  const saveBranding = async () => {
    setError(null);
    try {
      await api.tenant.updateBranding(branding);
      setSaved("Branding saved");
      refresh();
    } catch (e: any) {
      setError(e.message || "Failed to save branding");
    }
  };

  const saveSettings = async () => {
    setError(null);
    try {
      await api.tenant.updateSettings(settings);
      setSaved("Report settings saved");
      refresh();
    } catch (e: any) {
      setError(e.message || "Failed to save settings");
    }
  };

  return (
    <div className="max-w-4xl">
      <div className="flex items-center gap-3 mb-6">
        <Palette className="w-7 h-7 text-primary-600" />
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Workspace</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            How <span className="font-mono">{workspace?.workspace}</span> looks to your staff and on your reports.
          </p>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-4" />}
      {saved && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300">
          {saved}
        </div>
      )}

      <section className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 mb-6">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">Branding</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <label className="block text-sm">
            <span className="text-gray-600 dark:text-gray-300">Display name</span>
            <input className={inputCls} value={branding.display_name ?? ""}
              onChange={(e) => setBranding({ ...branding, display_name: e.target.value })} />
          </label>
          <div className="text-sm">
            <span className="text-gray-600 dark:text-gray-300">Logo</span>
            <div className="flex items-center gap-3 mt-1">
              {branding.logo_data_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={branding.logo_data_url} alt="Workspace logo" className="h-10 w-auto max-w-[8rem] object-contain rounded" />
              ) : (
                <span className="text-xs text-gray-400">No logo</span>
              )}
              <label className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 cursor-pointer">
                <Upload className="w-3.5 h-3.5" /> Upload
                <input type="file" accept="image/png,image/jpeg,image/webp" className="hidden"
                  onChange={(e) => onLogo(e.target.files?.[0])} />
              </label>
              {branding.logo_data_url && (
                <button className="text-xs text-red-600" onClick={() => setBranding({ ...branding, logo_data_url: null })}>
                  Remove
                </button>
              )}
            </div>
          </div>
          {(["primary_color", "accent_color"] as const).map((key) => (
            <label key={key} className="block text-sm">
              <span className="text-gray-600 dark:text-gray-300">{key === "primary_color" ? "Primary colour" : "Accent colour"}</span>
              <div className="flex items-center gap-2 mt-1">
                <input type="color" value={branding[key] || "#0ea5e9"}
                  onChange={(e) => setBranding({ ...branding, [key]: e.target.value })}
                  className="h-9 w-12 rounded border border-gray-200 dark:border-gray-700" />
                <input className={inputCls + " mt-0"} value={branding[key] ?? ""} placeholder="#RRGGBB"
                  onChange={(e) => setBranding({ ...branding, [key]: e.target.value || null })} />
              </div>
            </label>
          ))}
        </div>
        <div className="flex justify-end mt-4">
          <button onClick={saveBranding}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700">
            <Save className="w-4 h-4" /> Save branding
          </button>
        </div>
      </section>

      <section className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-1">Reports</h2>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
          Used on every PDF report this workspace produces. Empty fields fall back to the platform defaults.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {SETTINGS_FIELDS.map((f) => (
            <label key={f.key} className={`block text-sm ${f.multiline ? "md:col-span-2" : ""}`}>
              <span className="text-gray-600 dark:text-gray-300">{f.label}</span>
              {f.multiline ? (
                <textarea rows={2} className={inputCls} value={settings[f.key] ?? ""}
                  onChange={(e) => setSettings({ ...settings, [f.key]: e.target.value })} />
              ) : (
                <input className={inputCls} value={settings[f.key] ?? ""}
                  onChange={(e) => setSettings({ ...settings, [f.key]: e.target.value })} />
              )}
            </label>
          ))}
        </div>
        <div className="flex justify-end mt-4">
          <button onClick={saveSettings}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700">
            <Save className="w-4 h-4" /> Save report settings
          </button>
        </div>
      </section>
    </div>
  );
}
