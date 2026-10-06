"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type AuditReview, type AuditReviewEvent } from "@/lib/api";
import { Table, Caption, Th } from "@/components/ui/Table";
import { ClipboardCheck, CheckCircle2, AlertTriangle } from "lucide-react";

const inputCls =
  "w-full px-3 py-2 bg-white dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-accent";

/** "September 2026" for a monthly review, "Week of 21 Sept 2026" for a weekly one. */
function periodLabel(start: string | null, end?: string | null): string {
  if (!start) return "";
  const s = new Date(start);
  if (end && new Date(end).getTime() - s.getTime() <= 8 * 86400_000) {
    return `Week of ${s.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })}`;
  }
  return s.toLocaleDateString(undefined, { year: "numeric", month: "long", timeZone: "UTC" });
}

function Stat({ label, value, warn }: { label: string; value: number; warn?: boolean }) {
  return (
    <div className={`rounded-lg border p-3 ${warn && value > 0
      ? "border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40"
      : "border-gray-200 dark:border-gray-700 bg-white dark:bg-surface"}`}>
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
      <div className="text-xl font-semibold text-gray-900 dark:text-gray-100">{value}</div>
    </div>
  );
}

function Events({ title, rows }: { title: string; rows: AuditReviewEvent[] }) {
  return (
    <div className="mt-5">
      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
        {title} <span className="text-gray-400 font-normal">({rows.length})</span>
      </h3>
      {rows.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">None in this period.</p>
      ) : (
        <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 max-h-80 overflow-y-auto">
          <Table>
            <Caption>{title}</Caption>
            <thead>
              <tr><Th>When</Th><Th>Event</Th><Th>User</Th><Th>Details</Th><Th>From</Th></tr>
            </thead>
            <tbody>
              {rows.map((e, i) => (
                <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
                  <td className="py-1.5 px-3 whitespace-nowrap">{e.timestamp ? new Date(e.timestamp).toLocaleString() : ""}</td>
                  <td className="py-1.5 px-3">{e.action}</td>
                  <td className="py-1.5 px-3">{e.actor}</td>
                  <td className="py-1.5 px-3 text-xs break-all">
                    {Object.entries(e.details).map(([k, v]) => `${k}: ${String(v)}`).join(" · ") || `${e.entity_type}: ${e.entity_id}`}
                  </td>
                  <td className="py-1.5 px-3 font-mono text-xs">{e.client_ip}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </div>
  );
}

function Ranked({ title, rows }: { title: string; rows: { key: string; count: number }[] }) {
  return (
    <div>
      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">{title}</h3>
      {rows.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">None.</p>
      ) : (
        <ul className="text-sm space-y-1">
          {rows.map((r) => (
            <li key={r.key} className="flex justify-between gap-4">
              <span className="truncate">{r.key}</span>
              <span className="tabular-nums text-gray-500">{r.count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Monthly review of the audit log (HIPAA 164.308(a)(1)(ii)(D)): generated on the 1st
 * for the previous month; an administrator reads it and records the review. */
export default function AuditReviewPage() {
  const [reviews, setReviews] = useState<AuditReview[]>([]);
  const [selected, setSelected] = useState<AuditReview | null>(null);
  const [period, setPeriod] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setReviews((await api.auditReviews.list()).reviews);
    } catch (e: any) {
      setError(e.message || "Could not load reviews");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const open = async (id: string) => {
    setError("");
    try {
      setSelected(await api.auditReviews.get(id));
      setNotes("");
    } catch (e: any) {
      setError(e.message || "Could not load the review");
    }
  };

  const generate = async () => {
    setBusy(true);
    setError("");
    try {
      const review = await api.auditReviews.generate(period || undefined);
      await load();
      setSelected(review);
    } catch (e: any) {
      setError(e.message || "Could not generate the review");
    } finally {
      setBusy(false);
    }
  };

  const markReviewed = async () => {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      setSelected(await api.auditReviews.markReviewed(selected.id, notes));
      await load();
    } catch (e: any) {
      setError(e.message || "Could not record the review");
    } finally {
      setBusy(false);
    }
  };

  const s = selected?.summary;

  return (
    <div className="max-w-6xl">
      <div className="flex items-center gap-3 mb-2">
        <ClipboardCheck className="w-6 h-6 text-gray-500" />
        <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Audit review</h1>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">
        Each period&apos;s security-relevant activity, summarised from the audit log after the period
        closes (monthly on the 1st, or weekly on Mondays). Read it, follow up anything unexpected, and record the review.
      </p>

      {error && (
        <div className="mb-4 bg-red-50 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-lg text-sm">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[18rem_1fr] gap-6">
        <div className="space-y-3">
          <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-2">
            <label className="block">
              <span className="block text-xs text-gray-500 mb-1">Month (empty = last month)</span>
              <input type="month" value={period} onChange={(e) => setPeriod(e.target.value)} className={inputCls} />
            </label>
            <button onClick={generate} disabled={busy}
              className="w-full px-3 py-2 rounded-lg text-sm font-semibold bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50">
              {busy ? "Working…" : "Generate / open"}
            </button>
          </div>
          <ul className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 divide-y divide-gray-100 dark:divide-gray-800">
            {reviews.length === 0 && <li className="p-3 text-sm text-gray-500">No reviews yet.</li>}
            {reviews.map((r) => (
              <li key={r.id}>
                <button onClick={() => open(r.id)}
                  className={`w-full text-left p-3 text-sm hover:bg-gray-50 dark:hover:bg-white/5 ${selected?.id === r.id ? "bg-gray-50 dark:bg-white/5" : ""}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{periodLabel(r.period_start, r.period_end)}</span>
                    {r.reviewed_at
                      ? <CheckCircle2 className="w-4 h-4 text-emerald-600" aria-label="Reviewed" />
                      : <span className="text-xs text-amber-600">open</span>}
                  </div>
                  <div className="text-xs text-gray-500">
                    {r.findings_count} finding{r.findings_count === 1 ? "" : "s"}
                    {r.reviewed_by_username ? ` · reviewed by ${r.reviewed_by_username}` : ""}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div>
          {!selected || !s ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">Select a month.</p>
          ) : (
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{periodLabel(selected.period_start, selected.period_end)}</h2>
              <p className="text-xs text-gray-500 mb-4">
                {s.total_events} audit events · business hours {s.business_hours} ({s.timezone})
              </p>

              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Stat label="Failed sign-ins" value={s.failed_sign_ins.total} />
                <Stat label="Account lockouts" value={s.lockouts.length} warn />
                <Stat label="Emergency access" value={s.emergency_access.filter((e) => e.action === "break_glass_invoked").length} warn />
                <Stat label="Integrity alarms" value={s.integrity_alarms.length} warn />
                <Stat label="Patient-data reads" value={s.phi_reads} />
                <Stat label="…after hours" value={s.after_hours_phi_reads} warn />
                <Stat label="Session-theft alarms" value={s.session_alarms.length} warn />
                <Stat label="Exports / disclosures" value={Object.values(s.disclosures).reduce((a, b) => a + b, 0)} />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mt-6 bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-4">
                <Ranked title="Most active readers" rows={s.top_readers} />
                <Ranked title="After-hours readers" rows={s.top_after_hours_readers} />
                <Ranked title="Failed sign-ins by address" rows={s.failed_sign_ins.top_client_ips} />
              </div>

              <Events title="Account lockouts" rows={s.lockouts} />
              <Events title="Emergency access" rows={s.emergency_access} />
              <Events title="Integrity alarms" rows={s.integrity_alarms} />
              <Events title="Session-theft alarms" rows={s.session_alarms} />
              <Events title="Impersonation" rows={s.impersonations} />

              <div className="mt-6 bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-4">
                {selected.reviewed_at ? (
                  <div className="text-sm">
                    <div className="flex items-center gap-2 font-medium text-emerald-700 dark:text-emerald-400">
                      <CheckCircle2 className="w-4 h-4" />
                      Reviewed by {selected.reviewed_by_username} on {new Date(selected.reviewed_at).toLocaleString()}
                    </div>
                    {selected.review_notes && <p className="mt-2 whitespace-pre-wrap text-gray-700 dark:text-gray-300">{selected.review_notes}</p>}
                  </div>
                ) : (
                  <div className="space-y-3">
                    {s.findings > 0 && (
                      <p className="flex items-center gap-2 text-sm text-amber-700 dark:text-amber-400">
                        <AlertTriangle className="w-4 h-4" /> {s.findings} item{s.findings === 1 ? "" : "s"} to look at before signing off.
                      </p>
                    )}
                    <label className="block">
                      <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Review notes</span>
                      <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={4} maxLength={8000}
                        className={inputCls} placeholder="What was checked, what was followed up, outcome" />
                    </label>
                    <button onClick={markReviewed} disabled={busy}
                      className="btn-gradient px-4 py-2 rounded-lg text-sm font-semibold disabled:opacity-50">
                      Mark reviewed
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
