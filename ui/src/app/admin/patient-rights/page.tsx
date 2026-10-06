"use client";

import { useEffect, useState } from "react";
import {
  api,
  type PatientAccessEntry,
  type PatientAccessReport,
  type PatientRequest,
  type PatientRequestType,
  type PatientRestriction,
  type RestrictionChannel,
} from "@/lib/api";
import { Table, Caption, Th } from "@/components/ui/Table";
import { FileText, Download } from "lucide-react";

const inputCls =
  "w-full px-3 py-2 bg-white dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-accent";

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function EntryTable({ title, rows }: { title: string; rows: PatientAccessEntry[] }) {
  return (
    <div className="mt-6">
      <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
        {title} <span className="text-gray-400 font-normal">({rows.length})</span>
      </h2>
      {rows.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">None in this period.</p>
      ) : (
        <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 max-h-[28rem] overflow-y-auto">
          <Table>
            <Caption>{title}</Caption>
            <thead>
              <tr><Th>When</Th><Th>User</Th><Th>What</Th><Th>Resource</Th><Th>From</Th></tr>
            </thead>
            <tbody>
              {rows.map((e, i) => (
                <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
                  <td className="py-1.5 px-3 whitespace-nowrap">{e.timestamp ? new Date(e.timestamp).toLocaleString() : ""}</td>
                  <td className="py-1.5 px-3">{e.user}</td>
                  <td className="py-1.5 px-3">{e.action}{e.route ? ` · ${e.route}` : ""}</td>
                  <td className="py-1.5 px-3 font-mono text-xs break-all">{e.entity_type}: {e.entity_id}</td>
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

const REQUEST_TYPES: { value: PatientRequestType; label: string }[] = [
  { value: "access", label: "Copy of record (30 days)" },
  { value: "amendment", label: "Amendment (60 days)" },
  { value: "accounting", label: "Accounting of disclosures (60 days)" },
  { value: "restriction", label: "Restriction (30 days)" },
  { value: "confidential_communication", label: "Confidential communications (30 days)" },
];
const CHANNELS: { value: RestrictionChannel; label: string }[] = [
  { value: "all", label: "Every outbound channel" },
  { value: "fhir", label: "FHIR (EHR) push" },
  { value: "dicom_export", label: "DICOM SR / SEG export" },
  { value: "webhooks", label: "Webhooks" },
  { value: "share_links", label: "Share links" },
];
const fmtDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "");

/** Register of individual-rights requests with their statutory due dates (164.524(b)(2)). */
function RequestRegister({ mrn }: { mrn: string }) {
  const [rows, setRows] = useState<PatientRequest[]>([]);
  const [filter, setFilter] = useState("open");
  const [type, setType] = useState<PatientRequestType>("access");
  const [requester, setRequester] = useState("");
  const [details, setDetails] = useState("");
  const [err, setErr] = useState("");
  const load = () => api.patientRights.listRequests(filter || undefined).then(setRows).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [filter]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn: () => Promise<unknown>) => {
    setErr("");
    try { await fn(); load(); } catch (e: any) { setErr(e.message || "Request failed"); }
  };

  return (
    <section className="mt-8 bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Request register</h2>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} className="text-sm border border-gray-300 dark:border-white/10 dark:bg-white/5 rounded-lg px-2 py-1">
          <option value="open">Open</option>
          <option value="overdue">Overdue</option>
          <option value="fulfilled">Fulfilled</option>
          <option value="denied">Denied</option>
          <option value="">All</option>
        </select>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Log every request when it is received. One extension is allowed, before the due date, and the
        patient must be told the reason in writing.
      </p>
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      <div className="flex flex-wrap gap-2 mb-4">
        <select value={type} onChange={(e) => setType(e.target.value as PatientRequestType)} className={inputCls + " max-w-xs"}>
          {REQUEST_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
        <input value={requester} onChange={(e) => setRequester(e.target.value)} placeholder="Requested by (e.g. patient, ID checked)" className={inputCls + " max-w-xs"} maxLength={256} />
        <input value={details} onChange={(e) => setDetails(e.target.value)} placeholder="Details (optional)" className={inputCls + " max-w-xs"} maxLength={8000} />
        <button disabled={!mrn.trim() || requester.trim().length < 3}
          onClick={() => act(async () => {
            await api.patientRights.logRequest({ mrn: mrn.trim(), request_type: type, requester: requester.trim(), details: details.trim() || undefined });
            setRequester(""); setDetails("");
          })}
          className="btn-gradient px-4 py-2 rounded-lg text-sm font-semibold disabled:opacity-50">
          Log request for this MRN
        </button>
      </div>
      {rows.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">No requests.</p>
      ) : (
        <Table>
          <Caption>Patient requests</Caption>
          <thead><tr><Th>MRN</Th><Th>Type</Th><Th>Received</Th><Th>Due</Th><Th>Status</Th><Th>Action</Th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-gray-100 dark:border-gray-800 align-top">
                <td className="py-1.5 px-3 font-mono text-xs">{r.mrn}</td>
                <td className="py-1.5 px-3">{r.request_type.replace("_", " ")}<div className="text-xs text-gray-500">{r.requester}</div></td>
                <td className="py-1.5 px-3 whitespace-nowrap">{fmtDate(r.received_at)}</td>
                <td className={`py-1.5 px-3 whitespace-nowrap ${r.overdue ? "text-red-600 font-semibold" : ""}`}>
                  {fmtDate(r.due_at)}{r.overdue ? " · overdue" : ""}{r.extended_at ? " (extended)" : ""}
                </td>
                <td className="py-1.5 px-3">{r.status}{r.outcome_notes ? <div className="text-xs text-gray-500">{r.outcome_notes}</div> : null}</td>
                <td className="py-1.5 px-3 whitespace-nowrap space-x-2 text-xs">
                  {(r.status === "open" || r.status === "extended") && (
                    <>
                      <button className="text-emerald-700 hover:underline" onClick={() => {
                        const notes = prompt("How was it fulfilled?") ?? "";
                        act(() => api.patientRights.closeRequest(r.id, "fulfilled", notes));
                      }}>Fulfilled</button>
                      <button className="text-red-600 hover:underline" onClick={() => {
                        const notes = prompt("Reason for the denial (the patient must be told):");
                        if (notes) act(() => api.patientRights.closeRequest(r.id, "denied", notes));
                      }}>Deny</button>
                      {r.status === "open" && !r.overdue && (
                        <button className="text-gray-600 hover:underline" onClick={() => {
                          const reason = prompt("Reason for the delay (sent to the patient in writing):");
                          if (reason) act(() => api.patientRights.extendRequest(r.id, reason));
                        }}>Extend 30 days</button>
                      )}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </section>
  );
}

/** Agreed restrictions (164.522): enforced on every outbound channel they name. */
function Restrictions({ mrn }: { mrn: string }) {
  const [rows, setRows] = useState<PatientRestriction[]>([]);
  const [channel, setChannel] = useState<RestrictionChannel>("all");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState("");
  const m = mrn.trim();
  const load = () => (m ? api.patientRights.listRestrictions(m).then(setRows).catch((e) => setErr(e.message)) : setRows([]));
  useEffect(() => { load(); }, [m]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className="mt-6 bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5">
      <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Restrictions on disclosure</h2>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Once agreed, the platform refuses the restricted disclosures for this patient until the restriction is revoked or expires.
      </p>
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      {!m ? <p className="text-sm text-gray-500">Enter an MRN above.</p> : (
        <>
          <ul className="divide-y divide-gray-100 dark:divide-gray-800 text-sm mb-3">
            {rows.length === 0 && <li className="py-2 text-gray-400">No restrictions for this patient.</li>}
            {rows.map((r) => (
              <li key={r.id} className="py-2 flex items-start justify-between gap-3">
                <div>
                  <span className={r.active ? "font-medium" : "line-through text-gray-400"}>
                    {CHANNELS.find((c) => c.value === r.channel)?.label ?? r.channel}
                  </span>
                  <div className="text-xs text-gray-500">{r.reason} · agreed {fmtDate(r.agreed_at)} by {r.agreed_by}
                    {r.expires_at ? ` · until ${fmtDate(r.expires_at)}` : ""}{r.revoked_at ? ` · revoked ${fmtDate(r.revoked_at)}` : ""}</div>
                </div>
                {r.active && (
                  <button className="text-xs text-red-600 hover:underline" onClick={async () => {
                    if (!confirm("Revoke this restriction?")) return;
                    try { await api.patientRights.revokeRestriction(r.id); load(); } catch (e: any) { setErr(e.message); }
                  }}>Revoke</button>
                )}
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            <select value={channel} onChange={(e) => setChannel(e.target.value as RestrictionChannel)} className={inputCls + " max-w-xs"}>
              {CHANNELS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
            <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What was agreed" className={inputCls + " max-w-sm"} maxLength={4000} />
            <button disabled={reason.trim().length < 3} onClick={async () => {
              setErr("");
              try { await api.patientRights.addRestriction({ mrn: m, channel, reason: reason.trim() }); setReason(""); load(); }
              catch (e: any) { setErr(e.message); }
            }} className="px-4 py-2 rounded-lg text-sm font-semibold bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50">
              Add restriction
            </button>
          </div>
        </>
      )}
    </section>
  );
}

/** HIPAA patient rights: right of access (164.524) and accounting of disclosures /
 * access report (164.528). Both actions are themselves audited. */
export default function PatientRightsPage() {
  const [mrn, setMrn] = useState("");
  const [report, setReport] = useState<PatientAccessReport | null>(null);
  const [requestedBy, setRequestedBy] = useState("");
  const [includeImages, setIncludeImages] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [busy, setBusy] = useState<"" | "report" | "export">("");
  const [error, setError] = useState("");

  const run = async (kind: "report" | "export") => {
    setError("");
    setBusy(kind);
    try {
      if (kind === "report") {
        setReport(await api.patientRights.accessReport(mrn.trim()));
      } else {
        download(await api.patientRights.exportRecord(mrn.trim(), requestedBy.trim(), includeImages,
          passphrase || undefined), `patient_record_${mrn.trim()}.zip`);
        setPassphrase("");
      }
    } catch (e: any) {
      setError(e.message || "Request failed");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="max-w-5xl">
      <div className="flex items-center gap-3 mb-2">
        <FileText className="w-6 h-6 text-gray-500" />
        <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Patient rights</h1>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">
        Fulfil a patient&apos;s request for a copy of their record, or report who accessed and received
        their data. Verify the requester&apos;s identity first; both actions are recorded.
      </p>

      <div className="bg-white dark:bg-surface rounded-lg border border-gray-200 dark:border-gray-700 p-5 space-y-4">
        {error && (
          <div className="bg-red-50 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-lg text-sm">
            {error}
          </div>
        )}
        <label className="block max-w-sm">
          <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Patient MRN</span>
          <input value={mrn} onChange={(e) => setMrn(e.target.value)} className={inputCls} maxLength={64} />
        </label>

        <div className="flex flex-wrap gap-3">
          <button disabled={!mrn.trim() || !!busy} onClick={() => run("report")}
            className="px-4 py-2 rounded-lg text-sm font-semibold bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50">
            {busy === "report" ? "Loading…" : "Show access report"}
          </button>
          <button disabled={!mrn.trim() || !!busy}
            onClick={async () => {
              try {
                download(await api.patientRights.accessReportCsv(mrn.trim()), `access_report_${mrn.trim()}.csv`);
              } catch (e: any) {
                setError(e.message || "Request failed");
              }
            }}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-semibold bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50">
            <Download className="w-4 h-4" /> Download report (CSV)
          </button>
        </div>

        <div className="border-t border-gray-100 dark:border-gray-800 pt-4 space-y-3">
          <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Copy of the record (right of access)</h2>
          <label className="block max-w-lg">
            <span className="block text-sm text-gray-700 dark:text-gray-300 mb-1.5">Requested by</span>
            <input value={requestedBy} onChange={(e) => setRequestedBy(e.target.value)} className={inputCls}
              placeholder="e.g. Patient in person, ID checked" maxLength={256} />
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" checked={includeImages} onChange={(e) => setIncludeImages(e.target.checked)} />
            Include the original DICOM images (large)
          </label>
          <label className="block max-w-lg">
            <span className="block text-sm text-gray-700 dark:text-gray-300 mb-1.5">
              Passphrase (optional, at least 12 characters)
            </span>
            <input type="password" autoComplete="new-password" value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)} className={inputCls} maxLength={256} />
            <span className="block text-xs text-gray-500 dark:text-gray-400 mt-1">
              Encrypts the ZIP with AES-256 for a copy that leaves the building (USB stick, e-mail).
              Tell the patient the passphrase separately; it is not stored.
            </span>
          </label>
          <button disabled={!mrn.trim() || requestedBy.trim().length < 3 || (passphrase.length > 0 && passphrase.length < 12) || !!busy}
            onClick={() => run("export")}
            className="btn-gradient px-4 py-2 rounded-lg text-sm font-semibold disabled:opacity-50">
            {busy === "export" ? "Preparing…" : "Export patient record (ZIP)"}
          </button>
        </div>
      </div>

      {report && (
        <>
          <EntryTable title="Disclosures (data that left the workspace)" rows={report.disclosures} />
          <EntryTable title="Access (who viewed the data)" rows={report.accesses} />
        </>
      )}

      <Restrictions mrn={mrn} />
      <RequestRegister mrn={mrn} />
    </div>
  );
}
