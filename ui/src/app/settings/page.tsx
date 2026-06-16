"use client";

import { useHealth, useSiteConfig, useOrthancStudies, useUsecases } from "@/lib/hooks";
import {
  Settings,
  Heart,
  Server,
  Database,
  Brain,
  CheckCircle,
  XCircle,
  ExternalLink,
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
