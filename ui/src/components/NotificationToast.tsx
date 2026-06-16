"use client";

import { useState, useEffect, useCallback } from "react";
import { getWSClient } from "@/lib/ws";
import { X, CheckCircle, XCircle, Bell, Package, AlertTriangle } from "lucide-react";

interface Notification {
  id: string;
  type: string;
  message: string;
  timestamp: number;
}

export function NotificationToast() {
  const [notifications, setNotifications] = useState<Notification[]>([]);

  const addNotification = useCallback((type: string, message: string) => {
    const id = Math.random().toString(36).slice(2);
    setNotifications((prev) => [...prev.slice(-4), { id, type, message, timestamp: Date.now() }]);

    // Auto-dismiss after 8 seconds
    setTimeout(() => {
      setNotifications((prev) => prev.filter((n) => n.id !== id));
    }, 8000);
  }, []);

  useEffect(() => {
    const client = getWSClient();

    const unsub1 = client.subscribe("job_update", (msg) => {
      if (msg.status === "completed") {
        addNotification("success", `Job completed for study ${msg.study_instance_uid?.slice(0, 20)}...`);
      } else if (msg.status === "failed") {
        addNotification("error", `Job failed for study ${msg.study_instance_uid?.slice(0, 20)}...`);
      }
    });

    const unsub2 = client.subscribe("result_ready", (msg) => {
      addNotification("info", `New result: ${msg.usecase_name} for ${msg.study_instance_uid?.slice(0, 20)}...`);
    });

    const unsub3 = client.subscribe("alert", (msg) => {
      addNotification("warning", `Alert: ${msg.event_type}`);
    });

    const unsubCritical = client.subscribe("critical_finding", (msg) => {
      const severity = msg.severity === "CRITICAL" ? "critical" : "warning";
      addNotification(severity, `${msg.severity}: ${msg.title}`);
    });

    const unsubEscalated = client.subscribe("critical_finding_escalated", (msg) => {
      addNotification("critical", `ESCALATED (×${msg.escalation_count}): ${msg.title}`);
    });

    const unsub4 = client.subscribe("batch_progress", (msg) => {
      if (msg.status === "completed" || msg.status === "partial") {
        addNotification("info", `Batch ${msg.batch_id?.slice(0, 8)} finished: ${msg.completed}/${msg.total}`);
      }
    });

    return () => {
      unsub1();
      unsub2();
      unsub3();
      unsub4();
      unsubCritical();
      unsubEscalated();
    };
  }, [addNotification]);

  const dismiss = (id: string) => {
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  };

  const getIcon = (type: string) => {
    switch (type) {
      case "success": return <CheckCircle className="w-5 h-5 text-green-500" />;
      case "error": return <XCircle className="w-5 h-5 text-red-500" />;
      case "critical": return <AlertTriangle className="w-5 h-5 text-red-500" />;
      case "warning": return <Bell className="w-5 h-5 text-amber-500" />;
      default: return <Package className="w-5 h-5 text-blue-500" />;
    }
  };

  const getBorderColor = (type: string) => {
    if (type === "critical") return "border-red-300 bg-red-50";
    if (type === "error") return "border-red-200";
    if (type === "warning") return "border-amber-200";
    return "border-gray-200";
  };

  if (notifications.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {notifications.map((n) => (
        <div
          key={n.id}
          className={`bg-white rounded-lg shadow-lg border p-3 flex items-start gap-3 animate-in slide-in-from-right ${getBorderColor(n.type)}`}
        >
          {getIcon(n.type)}
          <p className="text-sm text-gray-700 flex-1">{n.message}</p>
          <button onClick={() => dismiss(n.id)} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>
      ))}
    </div>
  );
}
