"use client";

import { useEffect, useState } from "react";
import { useHealth, useSiteConfig, useOrthancStudies, useUsecases } from "@/lib/hooks";
import { api } from "@/lib/api";
import {
  Settings,
  Heart,
  Server,
  Database,
  Brain,
  CheckCircle,
  XCircle,
  ShieldCheck,
} from "lucide-react";

export default function SettingsPage() {
  const { data: health } = useHealth();
  const { data: siteConfig } = useSiteConfig();
  const { data: orthancStudies, error: orthancError } = useOrthancStudies();
  const { data: usecases } = useUsecases();

  const isHealthy = health?.status === "ok";
  const orthancConnected = !orthancError && orthancStudies !== undefined;

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-6">Settings</h1>

      {/* Integration Status */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <StatusCard
          icon={Server}
          label="Backend API"
          status={isHealthy}
          detail={health ? `v${health.version}` : "Checking..."}
        />
        <StatusCard
          icon={Database}
          label="Orthanc PACS"
          status={orthancConnected}
          detail={
            orthancConnected
              ? `${orthancStudies?.length ?? 0} studies`
              : "Disconnected"
          }
        />
        <StatusCard
          icon={Brain}
          label="AI Models"
          status={!!usecases?.length}
          detail={
            usecases
              ? `${usecases.filter((u) => u.enabled).length} enabled`
              : "Loading..."
          }
        />
      </div>

      {/* Site Configuration */}
      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 mb-6">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">Site Configuration</h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-500 mt-0.5">
            Current configuration for this deployment
          </p>
        </div>
        <div className="p-5">
          {siteConfig ? (
            <div className="space-y-3">
              <div className="flex items-center gap-3 text-sm">
                <span className="text-gray-500 dark:text-gray-400 dark:text-gray-500 w-24">Site ID:</span>
                <span className="font-medium text-gray-900 dark:text-gray-100">
                  {siteConfig.site_id || "default"}
                </span>
              </div>
              <div className="text-sm">
                <span className="text-gray-500 dark:text-gray-400 dark:text-gray-500">Configuration:</span>
                <pre className="mt-2 p-4 bg-gray-50 dark:bg-gray-800 dark:bg-surface-raised rounded-lg text-xs overflow-auto max-h-64 text-gray-700 dark:text-gray-300">
                  {JSON.stringify(siteConfig.config || siteConfig, null, 2)}
                </pre>
              </div>
            </div>
          ) : (
            <p className="text-sm text-gray-400 dark:text-gray-500">Loading configuration...</p>
          )}
        </div>
      </div>

      <MfaSettingsCard />

      {/* About */}
      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">About</h2>
        </div>
        <div className="p-5 space-y-2 text-sm">
          <div className="flex gap-3">
            <span className="text-gray-500 dark:text-gray-400 dark:text-gray-500 w-32">Platform:</span>
            <span className="text-gray-900 dark:text-gray-100">Cortex Radiology</span>
          </div>
          <div className="flex gap-3">
            <span className="text-gray-500 dark:text-gray-400 dark:text-gray-500 w-32">API Version:</span>
            <span className="text-gray-900 dark:text-gray-100">{health?.version || "-"}</span>
          </div>
          <div className="flex gap-3">
            <span className="text-gray-500 dark:text-gray-400 dark:text-gray-500 w-32">Models Registered:</span>
            <span className="text-gray-900 dark:text-gray-100">{usecases?.length ?? "-"}</span>
          </div>
          <div className="flex gap-3">
            <span className="text-gray-500 dark:text-gray-400 dark:text-gray-500 w-32">DICOM Viewer:</span>
            <span className="text-gray-900 dark:text-gray-100">
              OHIF (opens per study) · Orthanc Explorer is internal-only
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function MfaSettingsCard() {
  const [me, setMe] = useState<{ totp_enabled: boolean } | null>(null);
  const [enrollment, setEnrollment] = useState<{ secret: string; qr_code_data_uri: string } | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [disableCode, setDisableCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const refreshMe = () => {
    api.auth
      .me()
      .then((u) => setMe({ totp_enabled: !!u.totp_enabled }))
      .catch(() => setMe({ totp_enabled: false }));
  };

  useEffect(() => {
    refreshMe();
  }, []);

  const startEnroll = async () => {
    setError("");
    setLoading(true);
    try {
      const result = await api.auth.mfaEnroll();
      setEnrollment(result);
    } catch (e: any) {
      setError(e.message || "Failed to start MFA enrollment");
    } finally {
      setLoading(false);
    }
  };

  const confirmEnroll = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await api.auth.mfaConfirm(confirmCode);
      setRecoveryCodes(result.recovery_codes);
      setEnrollment(null);
      setConfirmCode("");
      refreshMe();
    } catch (e: any) {
      setError(e.message || "Invalid code");
    } finally {
      setLoading(false);
    }
  };

  const disable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await api.auth.mfaDisable(disableCode);
      setDisableCode("");
      refreshMe();
    } catch (e: any) {
      setError(e.message || "Invalid code");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 mb-6">
      <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2">
        <ShieldCheck className="w-4 h-4 text-gray-500 dark:text-gray-400" />
        <div>
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">Two-Factor Authentication</h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Require a time-based code in addition to your password when signing in
          </p>
        </div>
      </div>
      <div className="p-5">
        {error && (
          <div className="mb-4 bg-red-50 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-lg text-sm">
            {error}
          </div>
        )}

        {recoveryCodes ? (
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-3">
              MFA is now enabled. Save these recovery codes somewhere safe — each can be used once if you
              lose access to your authenticator app. They will not be shown again.
            </p>
            <div className="grid grid-cols-2 gap-2 font-mono text-sm bg-gray-50 dark:bg-surface-raised rounded-lg p-4 mb-4">
              {recoveryCodes.map((code) => (
                <span key={code} className="text-gray-800 dark:text-gray-200">{code}</span>
              ))}
            </div>
            <button
              onClick={() => setRecoveryCodes(null)}
              className="btn-gradient px-4 py-2 rounded-lg text-sm font-semibold"
            >
              Done
            </button>
          </div>
        ) : !me ? (
          <p className="text-sm text-gray-400 dark:text-gray-500">Loading...</p>
        ) : me.totp_enabled ? (
          <div>
            <p className="text-sm text-green-700 dark:text-green-400 mb-4 flex items-center gap-2">
              <CheckCircle className="w-4 h-4" /> Two-factor authentication is enabled on your account.
            </p>
            <form onSubmit={disable} className="flex items-end gap-3">
              <div className="flex-1 max-w-xs">
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                  Enter a code to disable
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  value={disableCode}
                  onChange={(e) => setDisableCode(e.target.value)}
                  className="w-full px-3 py-2 bg-white dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-lg text-sm"
                  placeholder="000000"
                  required
                />
              </div>
              <button
                type="submit"
                disabled={loading}
                className="px-4 py-2 rounded-lg text-sm font-semibold bg-red-600 hover:bg-red-700 text-white transition"
              >
                Disable MFA
              </button>
            </form>
          </div>
        ) : enrollment ? (
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-3">
              Scan this QR code with your authenticator app (Google Authenticator, 1Password, Authy, ...),
              then enter the 6-digit code it generates.
            </p>
            <div className="flex flex-col sm:flex-row gap-4 items-start mb-4">
              <img src={enrollment.qr_code_data_uri} alt="MFA QR code" className="w-40 h-40 rounded-lg border border-gray-200 dark:border-gray-700" />
              <div className="text-xs text-gray-500 dark:text-gray-400">
                <p className="mb-1">Can't scan? Enter this key manually:</p>
                <code className="block bg-gray-50 dark:bg-surface-raised px-2 py-1 rounded font-mono break-all">
                  {enrollment.secret}
                </code>
              </div>
            </div>
            <form onSubmit={confirmEnroll} className="flex items-end gap-3">
              <div className="flex-1 max-w-xs">
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                  Verification code
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  autoFocus
                  value={confirmCode}
                  onChange={(e) => setConfirmCode(e.target.value)}
                  className="w-full px-3 py-2 bg-white dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-lg text-sm"
                  placeholder="000000"
                  required
                />
              </div>
              <button
                type="submit"
                disabled={loading}
                className="btn-gradient px-4 py-2 rounded-lg text-sm font-semibold"
              >
                Confirm
              </button>
              <button
                type="button"
                onClick={() => {
                  setEnrollment(null);
                  setConfirmCode("");
                  setError("");
                }}
                className="px-4 py-2 rounded-lg text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition"
              >
                Cancel
              </button>
            </form>
          </div>
        ) : (
          <div>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              Two-factor authentication is not enabled on your account.
            </p>
            <button
              onClick={startEnroll}
              disabled={loading}
              className="btn-gradient px-4 py-2 rounded-lg text-sm font-semibold"
            >
              Enable Two-Factor Authentication
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function StatusCard({
  icon: Icon,
  label,
  status,
  detail,
}: {
  icon: any;
  label: string;
  status: boolean;
  detail: string;
}) {
  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Icon className="w-5 h-5 text-gray-600 dark:text-gray-400 dark:text-gray-500" />
          <span className="font-medium text-gray-900 dark:text-gray-100">{label}</span>
        </div>
        {status ? (
          <CheckCircle className="w-5 h-5 text-green-500 dark:text-green-400" />
        ) : (
          <XCircle className="w-5 h-5 text-red-400" />
        )}
      </div>
      <p className="text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500">{detail}</p>
    </div>
  );
}
