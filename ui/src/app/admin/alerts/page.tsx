"use client";

import { useState, useEffect, useCallback } from "react";
import { useAlertRules, useCriticalAlerts, useCriticalAlertStats } from "@/lib/hooks";
import { api, CriticalAlert } from "@/lib/api";
import { Plus, Trash2, Bell, AlertTriangle, CheckCircle, Clock, ChevronRight, ShieldAlert } from "lucide-react";
import { mutate } from "swr";
import { getWSClient } from "@/lib/ws";

type Tab = "critical" | "rules";

function SeverityBadge({ severity }: { severity: string }) {
  if (severity === "CRITICAL")
    return <span className="px-2 py-0.5 text-xs font-bold rounded-full bg-red-100 text-red-700">CRITICAL</span>;
  return <span className="px-2 py-0.5 text-xs font-bold rounded-full bg-amber-100 text-amber-700">WARNING</span>;
}

function StatusBadge({ status }: { status: string }) {
  switch (status) {
    case "pending":
      return <span className="px-2 py-0.5 text-xs rounded-full bg-yellow-50 text-yellow-700 flex items-center gap-1"><Clock className="w-3 h-3" />Pending</span>;
    case "acknowledged":
      return <span className="px-2 py-0.5 text-xs rounded-full bg-green-50 text-green-700 flex items-center gap-1"><CheckCircle className="w-3 h-3" />Acknowledged</span>;
    case "escalated":
      return <span className="px-2 py-0.5 text-xs rounded-full bg-red-50 text-red-700 flex items-center gap-1"><ShieldAlert className="w-3 h-3" />Escalated</span>;
    default:
      return <span className="px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-500">{status}</span>;
  }
}

function CriticalAlertsTab() {
  const [filter, setFilter] = useState<Record<string, string>>({});
  const { data: alerts, isLoading } = useCriticalAlerts(filter);
  const { data: stats } = useCriticalAlertStats();
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [ackingId, setAckingId] = useState<string | null>(null);
  const [ackUser, setAckUser] = useState("");

  // Real-time updates via WebSocket
  useEffect(() => {
    const client = getWSClient();
    const unsub = client.subscribe("critical_finding", () => {
      mutate(["critical-alerts", filter]);
      mutate("critical-alert-stats");
    });
    const unsub2 = client.subscribe("critical_finding_escalated", () => {
      mutate(["critical-alerts", filter]);
      mutate("critical-alert-stats");
    });
    return () => { unsub(); unsub2(); };
  }, [filter]);

  const handleAcknowledge = async (alert: CriticalAlert) => {
    if (!ackUser.trim()) return;
    setAckingId(alert.id);
    try {
      await api.criticalAlerts.acknowledge(alert.id, ackUser.trim());
      mutate(["critical-alerts", filter]);
      mutate("critical-alert-stats");
      setExpandedId(null);
    } finally {
      setAckingId(null);
    }
  };

  const unacked = stats?.total_unacknowledged ?? 0;

  return (
    <div>
      {/* Stats bar */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <p className="text-xs text-red-500 font-semibold uppercase tracking-wide">Critical Pending</p>
          <p className="text-3xl font-bold text-red-700 mt-1">{stats?.pending_critical ?? 0}</p>
        </div>
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
          <p className="text-xs text-amber-500 font-semibold uppercase tracking-wide">Warnings Pending</p>
          <p className="text-3xl font-bold text-amber-700 mt-1">{stats?.pending_warning ?? 0}</p>
        </div>
        <div className="bg-green-50 border border-green-200 rounded-lg p-4">
          <p className="text-xs text-green-500 font-semibold uppercase tracking-wide">Acknowledged</p>
          <p className="text-3xl font-bold text-green-700 mt-1">{stats?.total_acknowledged ?? 0}</p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-4">
        {["", "pending", "acknowledged", "escalated"].map((s) => (
          <button
            key={s}
            onClick={() => setFilter(s ? { status: s } : {})}
            className={`px-3 py-1.5 text-sm rounded-lg font-medium transition-colors ${
              (filter.status ?? "") === s
                ? "bg-primary-600 text-white"
                : "bg-white border border-gray-200 text-gray-600 hover:bg-gray-50"
            }`}
          >
            {s === "" ? "All" : s.charAt(0).toUpperCase() + s.slice(1)}
          </button>
        ))}
      </div>

      {/* Acknowledge user input */}
      <div className="mb-4 flex items-center gap-3">
        <label className="text-sm text-gray-600 font-medium shrink-0">Acknowledging as:</label>
        <input
          value={ackUser}
          onChange={(e) => setAckUser(e.target.value)}
          placeholder="Your name or username"
          className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 w-56"
        />
      </div>

      {/* Alert list */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden divide-y divide-gray-100">
        {isLoading && <div className="p-12 text-center text-gray-400">Loading...</div>}
        {!isLoading && (!alerts || alerts.length === 0) && (
          <div className="p-12 text-center text-gray-400">
            <CheckCircle className="w-12 h-12 mx-auto mb-3 text-green-300" />
            <p className="font-medium">No critical findings</p>
            <p className="text-sm mt-1">All AI analyses are within normal parameters.</p>
          </div>
        )}
        {(alerts || []).map((alert) => (
          <div key={alert.id}>
            <button
              className="w-full text-left px-4 py-3 hover:bg-gray-50 transition-colors"
              onClick={() => setExpandedId(expandedId === alert.id ? null : alert.id)}
            >
              <div className="flex items-start gap-3">
                <AlertTriangle className={`w-5 h-5 mt-0.5 shrink-0 ${alert.severity === "CRITICAL" ? "text-red-500" : "text-amber-500"}`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-gray-900 text-sm">{alert.title}</span>
                    <SeverityBadge severity={alert.severity} />
                    <StatusBadge status={alert.status} />
                  </div>
                  <div className="flex gap-4 mt-1 text-xs text-gray-400">
                    <span>{alert.usecase_name.replace(/_/g, " ")}</span>
                    {alert.patient_id && <span>Patient: {alert.patient_id}</span>}
                    <span>{new Date(alert.created_at).toLocaleString()}</span>
                    {alert.escalation_count > 0 && (
                      <span className="text-red-400 font-medium">Escalated {alert.escalation_count}×</span>
                    )}
                  </div>
                </div>
                <ChevronRight className={`w-4 h-4 text-gray-400 transition-transform shrink-0 ${expandedId === alert.id ? "rotate-90" : ""}`} />
              </div>
            </button>

            {expandedId === alert.id && (
              <div className="px-12 pb-4 bg-gray-50 border-t border-gray-100">
                <p className="text-sm text-gray-700 mt-3 leading-relaxed">{alert.message}</p>
                {Object.keys(alert.details ?? {}).length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1">
                    {Object.entries(alert.details).map(([k, v]) => (
                      <div key={k} className="text-xs">
                        <span className="text-gray-400">{k.replace(/_/g, " ")}: </span>
                        <span className="text-gray-700 font-medium">{String(v)}</span>
                      </div>
                    ))}
                  </div>
                )}
                <div className="mt-3 flex items-center gap-2 text-xs text-gray-400">
                  <span>Study: {alert.study_instance_uid.slice(0, 30)}…</span>
                  {alert.acknowledged_by && (
                    <span className="text-green-600">Acked by {alert.acknowledged_by} at {new Date(alert.acknowledged_at!).toLocaleString()}</span>
                  )}
                </div>
                {alert.status === "pending" || alert.status === "escalated" ? (
                  <button
                    onClick={() => handleAcknowledge(alert)}
                    disabled={!ackUser.trim() || ackingId === alert.id}
                    className="mt-3 px-4 py-1.5 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
                  >
                    <CheckCircle className="w-4 h-4" />
                    {ackingId === alert.id ? "Acknowledging…" : "Acknowledge"}
                  </button>
                ) : null}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function AlertRulesTab() {
  const { data: rules, isLoading } = useAlertRules();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", event_type: "job_failed", webhook_url: "" });

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.alerts.create(form);
    setShowForm(false);
    setForm({ name: "", event_type: "job_failed", webhook_url: "" });
    mutate("alert-rules");
  };

  const handleDelete = async (id: string) => {
    if (confirm("Delete this alert rule?")) {
      await api.alerts.delete(id);
      mutate("alert-rules");
    }
  };

  return (
    <div>
      <div className="flex justify-end mb-4">
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700"
        >
          <Plus className="w-4 h-4" /> Add Rule
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 mb-4 flex flex-wrap gap-3 items-end">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Name</label>
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="text-sm border border-gray-200 rounded-lg px-3 py-2" required />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Event Type</label>
            <select value={form.event_type} onChange={(e) => setForm({ ...form, event_type: e.target.value })} className="text-sm border border-gray-200 rounded-lg px-3 py-2">
              <option value="job_failed">Job Failed</option>
              <option value="job_completed">Job Completed</option>
              <option value="result_ready">Result Ready</option>
              <option value="queue_depth">Queue Depth</option>
              <option value="batch_completed">Batch Completed</option>
            </select>
          </div>
          <div className="flex-1 min-w-[200px]">
            <label className="block text-xs font-medium text-gray-500 mb-1">Webhook URL</label>
            <input
              value={form.webhook_url}
              onChange={(e) => setForm({ ...form, webhook_url: e.target.value })}
              className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2"
              placeholder="https://…"
              required
            />
          </div>
          <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-green-600 rounded-lg hover:bg-green-700">Create</button>
        </form>
      )}

      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center text-gray-400">Loading…</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Name</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Event</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Webhook</th>
                <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Status</th>
                <th className="py-3 px-4" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(rules || []).map((rule) => (
                <tr key={rule.id} className="hover:bg-gray-50">
                  <td className="py-2.5 px-4 font-medium text-gray-900">
                    <div className="flex items-center gap-2"><Bell className="w-4 h-4 text-amber-500" />{rule.name}</div>
                  </td>
                  <td className="py-2.5 px-4">
                    <span className="px-2 py-0.5 bg-purple-50 text-purple-700 text-xs rounded-full">{rule.event_type}</span>
                  </td>
                  <td className="py-2.5 px-4 text-gray-500 text-xs max-w-[300px] truncate">{rule.webhook_url}</td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 text-xs rounded-full ${rule.is_active ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-500"}`}>
                      {rule.is_active ? "Active" : "Disabled"}
                    </span>
                  </td>
                  <td className="py-2.5 px-4">
                    <button onClick={() => handleDelete(rule.id)} className="text-red-500 hover:text-red-700">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
              {(!rules || rules.length === 0) && (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-gray-400">No alert rules configured</td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default function AlertsPage() {
  const [tab, setTab] = useState<Tab>("critical");
  const { data: stats } = useCriticalAlertStats();
  const unacked = stats?.total_unacknowledged ?? 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Alerts</h1>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-gray-200">
        <button
          onClick={() => setTab("critical")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
            tab === "critical"
              ? "border-red-500 text-red-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          <AlertTriangle className="w-4 h-4" />
          Critical Findings
          {unacked > 0 && (
            <span className="ml-1 px-1.5 py-0.5 text-[10px] font-bold bg-red-500 text-white rounded-full">
              {unacked}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab("rules")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
            tab === "rules"
              ? "border-primary-500 text-primary-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          <Bell className="w-4 h-4" />
          Webhook Rules
        </button>
      </div>

      {tab === "critical" ? <CriticalAlertsTab /> : <AlertRulesTab />}
    </div>
  );
}
