"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type BreakGlassGrant } from "@/lib/api";
import { Table, Caption, Th } from "@/components/ui/Table";
import { EmptyState } from "@/components/ui/EmptyState";
import { ShieldAlert } from "lucide-react";

/** Review of break-glass emergency access in the workspace (HIPAA emergency access
 * procedure): who opened which patient, why, and for how long. Active grants can be
 * revoked; what was opened under a grant is in the audit log (phi_accessed). */
export default function EmergencyAccessReviewPage() {
  const [grants, setGrants] = useState<BreakGlassGrant[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api.breakGlass.list().then((r) => setGrants(r.grants)).catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  const revoke = async (id: string) => {
    try {
      await api.breakGlass.revoke(id);
      load();
    } catch (e: any) {
      setError(e.message || "Could not revoke");
    }
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-2">
        <ShieldAlert className="w-6 h-6 text-red-600 dark:text-red-400" />
        <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Emergency access review</h1>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">
        Every break-glass use in this workspace. Check each reason is a genuine emergency; the audit
        log shows exactly what was opened during the grant.
      </p>
      {error && <p className="text-sm text-red-600 dark:text-red-400 mb-4">{error}</p>}
      {grants === null ? null : grants.length === 0 ? (
        <EmptyState icon={ShieldAlert} title="No emergency access used" description="Break-glass grants will appear here." />
      ) : (
        <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700">
          <Table>
            <Caption>Emergency access grants</Caption>
            <thead>
              <tr>
                <Th>When</Th>
                <Th>User</Th>
                <Th>Patient (MRN)</Th>
                <Th>Reason</Th>
                <Th>Status</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {grants.map((g) => (
                <tr key={g.id} className="border-t border-gray-100 dark:border-gray-800 align-top">
                  <td className="py-2 px-3 whitespace-nowrap">{g.created_at ? new Date(g.created_at).toLocaleString() : ""}</td>
                  <td className="py-2 px-3">{g.username}</td>
                  <td className="py-2 px-3 font-mono">{g.patient_id}</td>
                  <td className="py-2 px-3 max-w-md">{g.reason}</td>
                  <td className="py-2 px-3 whitespace-nowrap">
                    {g.active ? (
                      <span className="text-amber-700 dark:text-amber-300">active until {new Date(g.expires_at).toLocaleTimeString()}</span>
                    ) : g.revoked_at ? (
                      <span className="text-gray-500">revoked by {g.revoked_by}</span>
                    ) : (
                      <span className="text-gray-400">expired</span>
                    )}
                  </td>
                  <td className="py-2 px-3 text-right">
                    {g.active && (
                      <button onClick={() => revoke(g.id)}
                        className="px-3 py-1 text-xs font-medium rounded bg-red-600 hover:bg-red-700 text-white">
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </div>
  );
}
