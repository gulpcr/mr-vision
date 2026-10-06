"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type BreakGlassGrant } from "@/lib/api";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ShieldAlert } from "lucide-react";

const inputCls =
  "w-full px-3 py-2 bg-white dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-accent";

/** Break-glass: time-limited emergency access to one patient outside your referrals. */
export default function EmergencyAccessPage() {
  const [mrn, setMrn] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const [grants, setGrants] = useState<BreakGlassGrant[]>([]);
  const [granted, setGranted] = useState<BreakGlassGrant | null>(null);

  const load = useCallback(() => {
    api.breakGlass.mine().then((r) => setGrants(r.grants)).catch(() => setGrants([]));
  }, []);
  useEffect(load, [load]);

  const submit = async () => {
    setConfirming(false);
    setError("");
    try {
      const grant = await api.breakGlass.invoke(mrn.trim(), reason.trim());
      setGranted(grant);
      setMrn("");
      setReason("");
      load();
    } catch (e: any) {
      setError(e.message || "Emergency access could not be granted");
    }
  };

  return (
    <div className="max-w-3xl">
      <div className="flex items-center gap-3 mb-2">
        <ShieldAlert className="w-6 h-6 text-red-600 dark:text-red-400" />
        <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Emergency access</h1>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">
        Use only when a patient outside your referrals needs care now. Access lasts one hour,
        your reason and everything you open are recorded, and your workspace administrators are
        notified and review every use.
      </p>

      {granted && (
        <div className="mb-6 rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/40 dark:border-amber-800 px-4 py-3 text-sm text-amber-900 dark:text-amber-200">
          Emergency access to <span className="font-mono">{granted.patient_id}</span> is active until{" "}
          {new Date(granted.expires_at).toLocaleTimeString()}.{" "}
          <Link href="/worklist" className="underline font-medium">Open the worklist</Link> to see the
          patient&apos;s studies.
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          setConfirming(true);
        }}
        className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 space-y-4"
      >
        {error && (
          <div className="bg-red-50 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-lg text-sm">
            {error}
          </div>
        )}
        <label className="block">
          <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Patient MRN</span>
          <input value={mrn} onChange={(e) => setMrn(e.target.value)} className={inputCls} required maxLength={64} />
        </label>
        <label className="block">
          <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Reason for emergency access</span>
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} className={inputCls} rows={3}
            required minLength={20} maxLength={4000}
            placeholder="e.g. Patient in ED with suspected stroke; prior CT needed for comparison" />
        </label>
        <button type="submit" className="px-4 py-2 rounded-lg text-sm font-semibold bg-red-600 hover:bg-red-700 text-white">
          Request emergency access
        </button>
      </form>

      <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mt-8 mb-2">Your recent emergency access</h2>
      {grants.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">None.</p>
      ) : (
        <ul className="divide-y divide-gray-100 dark:divide-gray-800 text-sm">
          {grants.map((g) => (
            <li key={g.id} className="py-2 flex items-center justify-between gap-4">
              <span className="font-mono">{g.patient_id}</span>
              <span className="text-gray-500 dark:text-gray-400 truncate flex-1">{g.reason}</span>
              <span className={g.active ? "text-amber-700 dark:text-amber-300" : "text-gray-400"}>
                {g.active ? `active until ${new Date(g.expires_at).toLocaleTimeString()}` : g.revoked_at ? "revoked" : "expired"}
              </span>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        tier="modal"
        open={confirming}
        title="Confirm emergency access"
        consequence={`You are about to open patient ${mrn.trim()} outside your referrals for one hour. This is recorded and reviewed by your administrators.`}
        confirmLabel="Open for one hour"
        danger
        onConfirm={submit}
        onCancel={() => setConfirming(false)}
      />
    </div>
  );
}
