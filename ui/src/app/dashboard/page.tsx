"use client";

import { useStudies, useUsecases, useHealth } from "@/lib/hooks";
import { api } from "@/lib/api";
import { formatDate, formatPatientName } from "@/lib/format";
import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Database,
  Activity,
  CheckCircle,
  Heart,
  ArrowRight,
  Brain,
} from "lucide-react";

export default function DashboardPage() {
  const { data: studyData, isLoading: studiesLoading } = useStudies();
  const { data: usecases } = useUsecases();
  const { data: health } = useHealth();
  const [jobStats, setJobStats] = useState({ active: 0, completed: 0 });

  useEffect(() => {
    if (!studyData?.studies?.length) return;
    let active = 0;
    let completed = 0;

    async function loadStats() {
      const studies = studyData!.studies.slice(0, 20);
      for (const s of studies) {
        try {
          const { jobs } = await api.jobs.listByStudy(s.study_instance_uid);
          for (const j of jobs) {
            if (j.status === "completed") completed++;
            else if (j.status !== "failed" && j.status !== "cancelled") active++;
          }
        } catch {}
      }
      setJobStats({ active, completed });
    }
    loadStats();
  }, [studyData]);

  const stats = [
    {
      label: "Total Studies",
      value: studyData?.total ?? "-",
      icon: Database,
      color: "text-blue-600 bg-blue-50",
    },
    {
      label: "Active Jobs",
      value: jobStats.active,
      icon: Activity,
      color: "text-amber-600 bg-amber-50",
    },
    {
      label: "Completed Analyses",
      value: jobStats.completed,
      icon: CheckCircle,
      color: "text-green-600 bg-green-50",
    },
    {
      label: "System Health",
      value: health?.status === "ok" ? "Online" : "Offline",
      icon: Heart,
      color: health?.status === "ok" ? "text-green-600 bg-green-50" : "text-red-600 bg-red-50",
    },
  ];

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Dashboard</h1>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {stats.map((s) => {
          const Icon = s.icon;
          return (
            <div
              key={s.label}
              className="bg-white rounded-lg shadow-sm border border-gray-200 p-5"
            >
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-gray-500">{s.label}</p>
                  <p className="text-2xl font-bold text-gray-900 mt-1">{s.value}</p>
                </div>
                <div className={`p-3 rounded-lg ${s.color}`}>
                  <Icon className="w-6 h-6" />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Studies */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200">
          <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
            <h2 className="font-semibold text-gray-900">Recent Studies</h2>
            <Link
              href="/worklist"
              className="text-sm text-primary-600 hover:text-primary-700 flex items-center gap-1"
            >
              View all <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
          <div className="divide-y divide-gray-50">
            {studiesLoading ? (
              <div className="p-5 text-sm text-gray-400">Loading...</div>
            ) : studyData?.studies?.length ? (
              studyData.studies.slice(0, 6).map((s) => (
                <Link
                  key={s.study_instance_uid}
                  href={`/study/${s.study_instance_uid}`}
                  className="flex items-center justify-between px-5 py-3 hover:bg-gray-50 transition-colors"
                >
                  <div>
                    <p className="text-sm font-medium text-gray-900">
                      {formatPatientName(s.patient_name)}
                    </p>
                    <p className="text-xs text-gray-500">
                      {s.modality} &middot; {s.body_part_examined || "N/A"} &middot;{" "}
                      {formatDate(s.study_date)}
                    </p>
                  </div>
                  <ArrowRight className="w-4 h-4 text-gray-400" />
                </Link>
              ))
            ) : (
              <div className="p-5 text-sm text-gray-400">No studies found</div>
            )}
          </div>
        </div>

        {/* AI Models */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200">
          <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
            <h2 className="font-semibold text-gray-900">AI Models</h2>
            <Link
              href="/admin/usecases"
              className="text-sm text-primary-600 hover:text-primary-700 flex items-center gap-1"
            >
              Manage <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
          <div className="divide-y divide-gray-50">
            {usecases?.length ? (
              usecases.map((uc) => (
                <div key={uc.name} className="flex items-center justify-between px-5 py-3">
                  <div className="flex items-center gap-3">
                    <Brain className="w-5 h-5 text-primary-600" />
                    <div>
                      <p className="text-sm font-medium text-gray-900">
                        {uc.name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                      </p>
                      <p className="text-xs text-gray-500">v{uc.version} &middot; {uc.model_type}</p>
                    </div>
                  </div>
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      uc.enabled
                        ? "bg-green-50 text-green-700"
                        : "bg-gray-100 text-gray-500"
                    }`}
                  >
                    {uc.enabled ? "Enabled" : "Disabled"}
                  </span>
                </div>
              ))
            ) : (
              <div className="p-5 text-sm text-gray-400">No models registered</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
