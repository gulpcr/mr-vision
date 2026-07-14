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
  ExternalLink,
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
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Settings</h1>

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
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 mb-6">
        <div className="px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">Site Configuration</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Current configuration for this deployment
          </p>
        </div>
        <div className="p-5">
          {siteConfig ? (
            <div className="space-y-3">
              <div className="flex items-center gap-3 text-sm">
                <span className="text-gray-500 w-24">Site ID:</span>
                <span className="font-medium text-gray-900">
                  {siteConfig.site_id || "default"}
                </span>
              </div>
              <div className="text-sm">
                <span className="text-gray-500">Configuration:</span>
                <pre className="mt-2 p-4 bg-gray-50 rounded-lg text-xs overflow-auto max-h-64 text-gray-700">
                  {JSON.stringify(siteConfig.config || siteConfig, null, 2)}
                </pre>
              </div>
            </div>
          ) : (
            <p className="text-sm text-gray-400">Loading configuration...</p>
          )}
        </div>
      </div>

      {/* Two-factor authentication */}
      <MfaSettingsCard />

      {/* About */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200">
        <div className="px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">About</h2>
        </div>
        <div className="p-5 space-y-2 text-sm">
          <div className="flex gap-3">
            <span className="text-gray-500 w-32">Platform:</span>
            <span className="text-gray-900">MRI AI Platform</span>
          </div>
          <div className="flex gap-3">
            <span className="text-gray-500 w-32">API Version:</span>
            <span className="text-gray-900">{health?.version || "-"}</span>
          </div>
          <div className="flex gap-3">
            <span className="text-gray-500 w-32">Models Registered:</span>
            <span className="text-gray-900">{usecases?.length ?? "-"}</span>
          </div>
          <div className="flex gap-3">
            <span className="text-gray-500 w-32">DICOM Viewer:</span>
            <a
              href="/orthanc/ui/app/index.html"
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary-600 hover:text-primary-700 flex items-center gap-1"
            >
              Orthanc Stone Viewer <ExternalLink className="w-3.5 h-3.5" />
            </a>
          </div>
        </div>
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
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Icon className="w-5 h-5 text-gray-600" />
          <span className="font-medium text-gray-900">{label}</span>
        </div>
        {status ? (
          <CheckCircle className="w-5 h-5 text-green-500" />
        ) : (
          <XCircle className="w-5 h-5 text-red-400" />
        )}
      </div>
      <p className="text-sm text-gray-500">{detail}</p>
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

  const refreshMe = () => api.auth.me().then(setMe).catch(() => {});

  useEffect(() => {
    refreshMe();
  }, []);

  const startEnroll = async () => {
    setError("");
    setLoading(true);
    try {
      setEnrollment(await api.auth.mfaEnroll());
    } catch (e: any) {
      setError(e.message || "Could not start enrollment");
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
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 mb-6">
      <div className="px-5 py-4 border-b border-gray-100 flex items-center gap-2">
        <ShieldCheck className="w-5 h-5 text-gray-600" />
        <div>
          <h2 className="font-semibold text-gray-900">Two-Factor Authentication</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Require a code from an authenticator app in addition to your password
          </p>
        </div>
      </div>
      <div className="p-5">
        {error && (
          <div className="bg-red-50 text-red-700 px-4 py-3 rounded-lg text-sm mb-4">{error}</div>
        )}

        {recoveryCodes ? (
          <div>
            <p className="text-sm text-gray-700 mb-2">
              MFA is now enabled. Save these recovery codes somewhere safe — each can be used
              once if you lose access to your authenticator app. They will not be shown again.
            </p>
            <div className="grid grid-cols-2 gap-2 font-mono text-sm bg-gray-50 rounded-lg p-4 mb-4">
              {recoveryCodes.map((c) => (
                <span key={c}>{c}</span>
              ))}
            </div>
            <button
              onClick={() => setRecoveryCodes(null)}
              className="py-2 px-4 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 text-sm"
            >
              Done
            </button>
          </div>
        ) : !me ? (
          <p className="text-sm text-gray-400">Loading...</p>
        ) : me.totp_enabled ? (
          <div>
            <p className="text-sm text-green-700 mb-4 flex items-center gap-2">
              <CheckCircle className="w-4 h-4" /> Two-factor authentication is enabled.
            </p>
            <form onSubmit={disable} className="space-y-3 max-w-sm">
              <label className="block text-sm font-medium text-gray-700">
                Enter a code to disable MFA
              </label>
              <input
                type="text"
                inputMode="numeric"
                value={disableCode}
                onChange={(e) => setDisableCode(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                placeholder="123456"
                required
              />
              <button
                type="submit"
                disabled={loading}
                className="py-2 px-4 bg-red-600 text-white font-medium rounded-lg hover:bg-red-700 disabled:opacity-50 text-sm"
              >
                {loading ? "Disabling..." : "Disable MFA"}
              </button>
            </form>
          </div>
        ) : enrollment ? (
          <div>
            <p className="text-sm text-gray-600 mb-3">
              Scan this QR code with your authenticator app (Google Authenticator, Authy, 1Password, ...),
              or enter the secret manually.
            </p>
            <img src={enrollment.qr_code_data_uri} alt="MFA QR code" className="mb-3 w-48 h-48" />
            <p className="text-xs font-mono text-gray-500 mb-4 break-all">{enrollment.secret}</p>
            <form onSubmit={confirmEnroll} className="space-y-3 max-w-sm">
              <label className="block text-sm font-medium text-gray-700">
                Enter the 6-digit code from your app to confirm
              </label>
              <input
                type="text"
                inputMode="numeric"
                autoFocus
                value={confirmCode}
                onChange={(e) => setConfirmCode(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                placeholder="123456"
                required
              />
              <div className="flex gap-2">
                <button
                  type="submit"
                  disabled={loading}
                  className="py-2 px-4 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm"
                >
                  {loading ? "Confirming..." : "Confirm"}
                </button>
                <button
                  type="button"
                  onClick={() => { setEnrollment(null); setConfirmCode(""); setError(""); }}
                  className="py-2 px-4 text-gray-500 hover:text-gray-700 text-sm"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        ) : (
          <div>
            <p className="text-sm text-gray-500 mb-4">
              Two-factor authentication is not enabled for your account.
            </p>
            <button
              onClick={startEnroll}
              disabled={loading}
              className="py-2 px-4 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm"
            >
              {loading ? "Starting..." : "Enable Two-Factor Authentication"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
