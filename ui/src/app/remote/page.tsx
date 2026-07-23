"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Study, UrgencyScore } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Table, Caption, Th } from "@/components/ui/Table";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { SEVERITY_CONFIG, type SeverityTier } from "@/lib/design/severity";
import { Radio, Zap, Lock, RefreshCw } from "lucide-react";

const PRIORITY_TIER: Record<string, SeverityTier> = {
  STAT: "critical",
  HIGH: "high",
  NORMAL: "moderate",
  ROUTINE: "informational",
};

// Remote-site / no-on-site-radiologist console: what a local technologist sees
// while waiting for a reader, and the one action they can take today —
// escalate to the load-balanced pool of active radiologists (which may be
// reading from anywhere) via the existing auto-assign endpoint, now gated by
// the narrower study.escalate permission rather than the broader study.claim.
export default function RemoteConsolePage() {
  const { can } = useAuth();
  const [studies, setStudies] = useState<Study[]>([]);
  const [urgencyMap, setUrgencyMap] = useState<Record<string, UrgencyScore>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [escalatingUid, setEscalatingUid] = useState<string | null>(null);
  const [escalateError, setEscalateError] = useState<string | null>(null);

  const canEscalate = can("study.escalate");
  const canClaim = can("study.claim");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const { studies: all } = await api.studies.list({ limit: "100" });
      const pending = all.filter((s) => (s.reading_status || "unread") !== "signed");
      setStudies(pending);
      if (pending.length > 0) {
        const { scores } = await api.metrics.getUrgencyScores(pending.map((s) => s.study_instance_uid));
        const map: Record<string, UrgencyScore> = {};
        for (const s of scores) map[s.study_instance_uid] = s;
        setUrgencyMap(map);
      }
    } catch (e: any) {
      setError(e.message || "Failed to load studies");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const sorted = [...studies].sort((a, b) => {
    const oa = PRIORITY_TIER[urgencyMap[a.study_instance_uid]?.priority ?? "ROUTINE"];
    const ob = PRIORITY_TIER[urgencyMap[b.study_instance_uid]?.priority ?? "ROUTINE"];
    const tierOrder: SeverityTier[] = ["critical", "high", "moderate", "informational", "good"];
    const diff = tierOrder.indexOf(oa) - tierOrder.indexOf(ob);
    if (diff !== 0) return diff;
    return (urgencyMap[b.study_instance_uid]?.score ?? 0) - (urgencyMap[a.study_instance_uid]?.score ?? 0);
  });

  const handleEscalate = async (uid: string) => {
    setEscalatingUid(uid);
    setEscalateError(null);
    try {
      await api.reading.autoAssign(uid);
      await load();
    } catch (e: any) {
      setEscalateError(e.message || "Escalation failed");
    } finally {
      setEscalatingUid(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <Radio className="w-7 h-7 text-primary-600 shrink-0 mt-0.5" />
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Remote Reading Console</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
            For sites with no on-site radiologist — track studies awaiting a reader and escalate to the
            on-call pool. {!canClaim && "You can escalate a study to a radiologist, but claiming, reporting, and signing off require a radiologist or admin account."}
          </p>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      {escalateError && <ErrorBanner message={escalateError} onDismiss={() => setEscalateError(null)} />}

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <Table>
          <Caption>Studies awaiting a radiologist reader, sorted by urgency</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Priority</Th>
              <Th>Patient</Th>
              <Th>Study Date</Th>
              <Th>Reading Status</Th>
              <Th className="w-40" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {loading ? (
              <TableSkeleton rows={6} columnWidths={[70, 130, 90, 100, 130]} />
            ) : sorted.length === 0 ? (
              <tr>
                <td colSpan={5}>
                  <EmptyState
                    icon={Radio}
                    title="No studies waiting on a reader"
                    description="Everything at this site has been claimed, reported, or signed off."
                  />
                </td>
              </tr>
            ) : (
              sorted.map((s) => {
                const urgency = urgencyMap[s.study_instance_uid];
                const tier = PRIORITY_TIER[urgency?.priority ?? "ROUTINE"];
                const cfg = SEVERITY_CONFIG[tier];
                const Icon = cfg.icon;
                const rs = s.reading_status || "unread";
                return (
                  <tr key={s.study_instance_uid} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                    <td className="py-2.5 px-4">
                      <span className={`inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full ${cfg.badgeClass}`}>
                        <Icon className="w-3.5 h-3.5" /> {urgency?.priority ?? "ROUTINE"}
                      </span>
                    </td>
                    <td className="py-2.5 px-4">
                      <Link href={`/study/${s.study_instance_uid}`} className="font-medium text-gray-900 dark:text-gray-100 hover:underline">
                        {s.patient_name || "—"}
                      </Link>
                    </td>
                    <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{s.study_date || "—"}</td>
                    <td className="py-2.5 px-4">
                      <StatusBadge variant="reading" status={rs} assignedTo={s.assigned_to_username} />
                    </td>
                    <td className="py-2.5 px-4">
                      {canEscalate ? (
                        <button
                          onClick={() => handleEscalate(s.study_instance_uid)}
                          disabled={escalatingUid === s.study_instance_uid || rs === "reported"}
                          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 rounded-lg transition-colors"
                        >
                          {escalatingUid === s.study_instance_uid ? (
                            <RefreshCw className="w-3.5 h-3.5 animate-spin motion-reduce:animate-none" />
                          ) : (
                            <Zap className="w-3.5 h-3.5" />
                          )}
                          Escalate
                        </button>
                      ) : (
                        <span className="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500">
                          <Lock className="w-3.5 h-3.5" /> Radiologist only
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </Table>
      </div>
    </div>
  );
}
